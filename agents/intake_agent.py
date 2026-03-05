"""
CONDUIT — Intake Agent (Agent 1)
==================================
Entry point of the CONDUIT pipeline.

Responsibilities:
    1. Decode VIN → get vehicle details
    2. Look up customer by VIN → get loyalty tier
    3. Check active recalls for this vehicle
    4. Semantic search on Pinecone → find relevant parts
    5. Call GPT-4o → classify fault, confirm parts, set urgency
    6. Write classification back to RepairOrder in PostgreSQL
    7. Return enriched state for Inventory Agent

Input state fields used:
    - ro_id, vin, complaint_text, customer_id

Output state fields added:
    - vehicle_details
    - customer_details
    - recall_flags
    - retrieved_parts_context  (raw Pinecone results)
    - fault_classification
    - required_parts
    - urgency
    - intake_confidence
    - error (if something went wrong)
"""

import os
import json
import time
from typing import Optional

from dotenv import load_dotenv
load_dotenv()


# ── IMPORT TOOLS ──────────────────────────────────────────────────────────────

from tools.vehicle_tools import decode_vin, get_customer_by_vin, check_recall
from tools.pinecone_tools import search_parts_catalog
from database.connection import get_session
from database.models import RepairOrder

# ── IMPORT LOGGER ─────────────────────────────────────────────────────────────

from app_logging.agent_logger import (
    log_agent_start,
    log_agent_end,
    log_agent_error,
    log_guardrail_failure,
)


# ── LLM CLASSIFICATION ────────────────────────────────────────────────────────

def classify_fault_with_llm(
    complaint_text: str,
    vehicle: dict,
    retrieved_parts: list,
    recall_flags: list,
) -> dict:
    """
    Calls GPT-4o to classify the fault and confirm required parts.

    The LLM receives:
    - Raw complaint text from service advisor
    - Vehicle details (make, model, year, fuel type)
    - Top 5 semantically relevant parts from Pinecone
    - Any active recall flags

    It returns structured JSON with classification,
    confirmed parts list, urgency, and confidence score.
    """
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # Format retrieved parts for the prompt
    parts_context = "\n".join([
        f"- {p['part_number']}: {p['description']} "
        f"(Stock: {p['qty_on_hand']} units, "
        f"Status: {p['stock_status']}, "
        f"Price: ₹{p['sell_price']})"
        for p in retrieved_parts
    ])

    # Format recall flags
    recall_context = ""
    if recall_flags:
        recall_context = "\n\nACTIVE RECALLS FOR THIS VEHICLE:\n"
        for r in recall_flags:
            recall_context += (
                f"- {r['recall_id']}: {r['description']} "
                f"[Severity: {r['severity']}]\n"
            )

    # System prompt — instructs GPT-4o to act as automotive expert
    system_prompt = """You are an expert automotive service advisor AI 
for an Indian dealership. Your job is to classify vehicle complaints 
and identify required parts for repair.

You must respond ONLY with valid JSON — no explanation, no markdown, 
no preamble. Just the JSON object.

Fault categories you can use:
- BRAKE_SYSTEM
- ROUTINE_SERVICE  
- ELECTRICAL_SYSTEM
- SUSPENSION
- ENGINE
- EV_SYSTEM
- UNKNOWN

Urgency levels:
- HIGH (safety critical — must fix today)
- MEDIUM (should fix within a week)
- LOW (can be scheduled)
- NEEDS_CLARIFICATION (complaint too vague)"""

    # User prompt — the actual classification request
    user_prompt = """Classify this vehicle complaint and identify required parts.

VEHICLE:
- Make/Model: {vehicle['make']} {vehicle['model']} {vehicle['year']}
- Fuel Type: {vehicle['fuel_type']}
- Engine: {vehicle['engine_code']}
- Odometer: {vehicle.get('odometer_km', 'unknown')} km
- Warranty Expired: {vehicle['warranty_expired']}
- Is EV: {vehicle['is_ev']}

COMPLAINT FROM SERVICE ADVISOR:
"{complaint_text}"

RELEVANT PARTS FROM CATALOG (retrieved by semantic search):
{parts_context}
{recall_context}

Based on the complaint and vehicle details, respond with this exact JSON:
{{
    "fault_classification": "BRAKE_SYSTEM",
    "fault_description": "One sentence describing the likely fault",
    "required_parts": ["BRK-PAD-HON-F-01", "BRK-ROT-HON-F-01"],
    "recommended_labor_codes": ["BRK-001", "BRK-002"],
    "urgency": "HIGH",
    "urgency_reason": "Safety critical — brake failure risk",
    "confidence": 0.92,
    "recall_action_required": false,
    "technician_skill_required": "Technician",
    "ev_safety_protocol": false,
    "notes": "Any additional notes for the technician"
}}

Rules:
- Only include parts from the provided catalog list
- Set ev_safety_protocol to true for any EV high voltage work
- Set recall_action_required to true if an active recall matches this complaint
- confidence should reflect how clear the complaint is (0.0 to 1.0)
- If complaint is too vague, set fault_classification to UNKNOWN and urgency to NEEDS_CLARIFICATION"""

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL_NAME", "gpt-4o"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,   # low temperature = more deterministic classification
        max_tokens=500,
    )

    raw_response = response.choices[0].message.content
    classification = json.loads(raw_response)

    # Add token usage for LangSmith cost tracking
    classification["_token_usage"] = {
        "prompt_tokens":     response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens":      response.usage.total_tokens,
    }

    return classification


# ── GUARDRAILS ────────────────────────────────────────────────────────────────

def validate_intake_output(
    agent_output: dict,
    ro_id: str
) -> tuple:
    """
    Validates Intake Agent output before writing to shared state.
    Returns (is_valid: bool, reason: str)
    """

    # 1. Required fields must be present
    required_fields = [
        "fault_classification",
        "required_parts",
        "urgency",
        "confidence",
    ]
    for field in required_fields:
        if field not in agent_output:
            return False, f"Missing required field: {field}"

    # 2. Confidence must be float between 0 and 1
    confidence = agent_output.get("confidence", 0)
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
        return False, f"Invalid confidence value: {confidence}"

    # 3. Urgency must be from allowed set
    allowed_urgency = {
        "HIGH", "MEDIUM", "LOW", "NEEDS_CLARIFICATION"
    }
    if agent_output.get("urgency") not in allowed_urgency:
        return False, f"Invalid urgency: {agent_output.get('urgency')}"

    # 4. Fault classification must be from allowed set
    allowed_categories = {
        "BRAKE_SYSTEM", "ROUTINE_SERVICE", "ELECTRICAL_SYSTEM",
        "SUSPENSION", "ENGINE", "EV_SYSTEM", "UNKNOWN"
    }
    if agent_output.get("fault_classification") not in allowed_categories:
        return False, (
            "Invalid fault category: "
            f"{agent_output.get('fault_classification')}"
        )

    # 5. Required parts must be a list (can be empty for UNKNOWN)
    if not isinstance(agent_output.get("required_parts"), list):
        return False, "required_parts must be a list"

    # 6. Safety check — brake/EV complaints must never be LOW urgency
    fault = agent_output.get("fault_classification", "")
    urgency = agent_output.get("urgency", "")

    if fault == "BRAKE_SYSTEM" and urgency == "LOW":
        return False, (
            "Brake system fault cannot have LOW urgency — "
            "safety override required"
        )

    if fault == "EV_SYSTEM" and urgency == "LOW":
        return False, (
            "EV system fault cannot have LOW urgency — "
            "safety override required"
        )

    return True, "OK"


# ── PERSIST TO DATABASE ───────────────────────────────────────────────────────

def write_classification_to_db(
    ro_id: str,
    vehicle: dict,
    customer: Optional[dict],
    classification: dict,
) -> bool:
    """
    Writes Intake Agent results back to the repair_orders table.
    Updates the classification_payload JSONB column.
    """
    try:
        with get_session() as db:
            ro = db.query(RepairOrder).filter(
                RepairOrder.ro_id == ro_id
            ).first()

            if ro:
                ro.fault_category = classification["fault_classification"]
                ro.classification_payload = classification
                ro.vehicle_make  = vehicle["make"]
                ro.vehicle_model = vehicle["model"]
                ro.vehicle_year  = vehicle["year"]
                ro.vehicle_fuel_type = vehicle["fuel_type"]
                ro.is_ev_job = (
                    vehicle["is_ev"] or
                    classification["fault_classification"] == "EV_SYSTEM"
                )
                if customer:
                    ro.customer_id   = customer["customer_id"]
                    ro.customer_name = customer["full_name"]

                db.commit()
                return True

        return False

    except Exception:
        return False


# ── MAIN AGENT FUNCTION ───────────────────────────────────────────────────────

def run_intake_agent(state: dict) -> dict:
    """
    Main Intake Agent function — called by LangGraph orchestrator.

    Takes the shared state dict, adds its outputs, returns updated state.
    Never modifies state in place — always returns a new dict with additions.
    """
    start_time = time.time()
    ro_id = state.get("ro_id", "UNKNOWN")

    log_agent_start(
        agent_name="intake_agent",
        ro_id=ro_id,
        input_summary={
            "vin":       state.get("vin"),
            "complaint": state.get("complaint_text", "")[:80],
        }
    )

    try:

        # ── STEP 1: DECODE VIN ─────────────────────────────────────────────
        vin = state.get("vin", "").upper().strip()

        if not vin:
            raise ValueError("VIN is required but was not provided")

        vehicle = decode_vin(vin)

        if not vehicle:
            raise ValueError(
                f"VIN {vin} not found in vehicle catalog. "
                "Vehicle may not be registered in the system."
            )

        # ── STEP 2: LOOK UP CUSTOMER ───────────────────────────────────────
        customer = get_customer_by_vin(vin)
        # Customer is optional — walk-in customers won't be in DB

        # ── STEP 3: CHECK RECALLS ──────────────────────────────────────────
        recall_flags = check_recall(
            vin=vin,
            make=vehicle["make"],
            model=vehicle["model"],
            year=vehicle["year"],
        )

        # ── STEP 4: SEMANTIC SEARCH ON PINECONE ───────────────────────────
        complaint_text = state.get("complaint_text", "")

        if not complaint_text:
            raise ValueError("Complaint text is required but was not provided")

        # Search with vehicle make filter for more accurate results
        retrieved_parts = search_parts_catalog(
            complaint_text=complaint_text,
            top_k=5,
            filter_make=vehicle["make"]
            if vehicle["make"] != "Unknown" else None,
            filter_fuel_type=vehicle["fuel_type"]
            if vehicle["is_ev"] else None,
        )

        # Fallback: if filtered search returns < 3 results,
        # try without filter to ensure we have enough context
        if len(retrieved_parts) < 3:
            retrieved_parts = search_parts_catalog(
                complaint_text=complaint_text,
                top_k=5,
            )

        # ── STEP 5: LLM CLASSIFICATION ────────────────────────────────────
        classification = classify_fault_with_llm(
            complaint_text=complaint_text,
            vehicle=vehicle,
            retrieved_parts=retrieved_parts,
            recall_flags=recall_flags,
        )

        # ── STEP 6: GUARDRAIL VALIDATION ──────────────────────────────────
        is_valid, reason = validate_intake_output(classification, ro_id)

        if not is_valid:
            log_guardrail_failure("intake_agent", ro_id, reason)

            # Safety override — don't fail the pipeline,
            # route to human review instead
            classification["urgency"]              = "NEEDS_CLARIFICATION"
            classification["_guardrail_triggered"] = True
            classification["_guardrail_reason"]    = reason

        # ── STEP 7: WRITE TO DATABASE ──────────────────────────────────────
        write_classification_to_db(
            ro_id=ro_id,
            vehicle=vehicle,
            customer=customer,
            classification=classification,
        )

        # ── STEP 8: BUILD OUTPUT STATE ────────────────────────────────────
        latency_ms = int((time.time() - start_time) * 1000)

        log_agent_end(
            agent_name="intake_agent",
            ro_id=ro_id,
            output_summary={
                "fault":      classification.get("fault_classification"),
                "urgency":    classification.get("urgency"),
                "confidence": classification.get("confidence"),
                "parts":      classification.get("required_parts", []),
                "recalls":    len(recall_flags),
                "latency_ms": latency_ms,
            },
            latency_ms=latency_ms,
        )

        # Return updated state — all previous state preserved + new fields added
        return {
            **state,

            # Vehicle and customer context
            "vehicle_details":          vehicle,
            "customer_details":         customer,
            "recall_flags":             recall_flags,

            # RAG context — kept for explainability + evals
            "retrieved_parts_context":  retrieved_parts,

            # LLM classification outputs
            "fault_classification":     classification.get("fault_classification"),
            "fault_description":        classification.get("fault_description"),
            "required_parts":           classification.get("required_parts", []),
            "recommended_labor_codes":  classification.get("recommended_labor_codes", []),
            "urgency":                  classification.get("urgency"),
            "urgency_reason":           classification.get("urgency_reason"),
            "intake_confidence":        classification.get("confidence", 0.0),
            "recall_action_required":   classification.get("recall_action_required", False),
            "technician_skill_required": classification.get("technician_skill_required"),
            "ev_safety_protocol":       classification.get("ev_safety_protocol", False),
            "intake_notes":             classification.get("notes"),

            # Clear any previous error
            "error":                    None,
            "current_agent":            "intake_agent",
        }

    except Exception:
        latency_ms = int((time.time() - start_time) * 1000)

        log_agent_error(
            agent_name="intake_agent",
            ro_id=ro_id,
            error=str(e),
            input_state={
                "vin":       state.get("vin"),
                "complaint": state.get("complaint_text", "")[:80],
            }
        )

        return {
            **state,
            "error":         str(e),
            "current_agent": "intake_agent",
        }


# ── STANDALONE TEST ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Quick test without running the full pipeline.
    Run: python agents/intake_agent.py
    Requires: PostgreSQL running + Pinecone seeded
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))

    # Pull a real VIN from the database for testing
    from database.connection import get_session
    from database.models import Vehicle

    with get_session() as db:
        sample_vehicle = db.query(Vehicle).first()

    if not sample_vehicle:
        print("No vehicles in database. Run generate_all.py first.")
        sys.exit(1)

    # Test state
    test_state = {
        "ro_id":          "RO-TEST-001",
        "vin":            sample_vehicle.vin,
        "complaint_text": "grinding noise from front wheels when braking, "
                         "car pulls to the left",
        "customer_id":    None,
    }

    print("\nRunning Intake Agent test...")
    print(f"VIN: {test_state['vin']}")
    print(f"Vehicle: {sample_vehicle.year} {sample_vehicle.make} "
          f"{sample_vehicle.model}")
    print(f"Complaint: {test_state['complaint_text']}\n")

    result = run_intake_agent(test_state)

    if result.get("error"):
        print(f"ERROR: {result['error']}")
    else:
        print(f"Fault Classification: {result['fault_classification']}")
        print(f"Required Parts:       {result['required_parts']}")
        print(f"Urgency:              {result['urgency']}")
        print(f"Confidence:           {result['intake_confidence']}")
        print(f"Recall Flags:         {len(result['recall_flags'])} active")
        print(f"EV Safety Protocol:   {result['ev_safety_protocol']}")
        print("\nRetrieved Parts from Pinecone:")
        for p in result.get("retrieved_parts_context", []):
            print(f"  [{p['similarity_score']}] "
                  f"{p['part_number']} — {p['description'][:60]}")