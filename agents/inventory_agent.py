"""
CONDUIT — Inventory Agent (Agent 2)
=====================================
Receives classified RO from Intake Agent.
Checks stock, validates compatibility, reserves parts.

Responsibilities:
    1. Receive required_parts list from Intake Agent
    2. For each part — fetch full details from PostgreSQL
    3. Validate compatibility with vehicle (make/model/year/fuel)
    4. Check available stock (qty_on_hand - qty_reserved)
    5. Reserve parts atomically (prevent race conditions)
    6. Flag parts that fall below reorder point
    7. Return inventory check results for Quoting Agent

Input state fields used:
    - ro_id
    - required_parts        (from Intake Agent)
    - vehicle_details       (from Intake Agent)
    - retrieved_parts_context (Pinecone results — for fallback)

Output state fields added:
    - inventory_check       (detailed per-part results)
    - parts_available       (bool — all required parts in stock)
    - reserved_parts        (list of successfully reserved parts)
    - unavailable_parts     (list of parts with zero stock)
    - reorder_needed        (list of parts below reorder point)
    - error                 (if something went wrong)
"""

import time
from typing import List, Dict, Optional

from tools.inventory_tools import (
    get_part_details,
    check_compatibility,
    check_stock,
    reserve_parts,
    check_reorder_needed,
)

from app_logging.agent_logger import (
    log_agent_start,
    log_agent_end,
    log_agent_error,
    log_guardrail_failure,
)


# ── GUARDRAILS ────────────────────────────────────────────────────────────────

def validate_inventory_output(
    agent_output: dict,
    ro_id: str,
) -> tuple:
    """
    Validates Inventory Agent output before passing to Quoting Agent.
    Returns (is_valid, reason)
    """

    # 1. parts_available must be boolean
    if not isinstance(agent_output.get("parts_available"), bool):
        return False, "parts_available must be boolean"

    # 2. reserved_parts must be a list
    if not isinstance(agent_output.get("reserved_parts", []), list):
        return False, "reserved_parts must be a list"

    # 3. Every reserved part must have required fields
    for part in agent_output.get("reserved_parts", []):
        for field in ["part_number", "qty_reserved", "unit_cost", "sell_price"]:
            if field not in part:
                return False, (
                    f"Reserved part missing required field: {field}"
                )

    # 4. No negative quantities
    for part in agent_output.get("reserved_parts", []):
        if part.get("qty_reserved", 0) <= 0:
            return False, (
                "Invalid reservation quantity for "
                f"{part.get('part_number')}: "
                f"{part.get('qty_reserved')}"
            )

    return True, "OK"


# ── FIND ALTERNATIVE PARTS ────────────────────────────────────────────────────

def find_alternative_part(
    original_part_number: str,
    vehicle: Dict,
    retrieved_parts_context: List[Dict],
) -> Optional[Dict]:
    """
    When a required part is out of stock, looks for an alternative.

    Strategy:
    1. Check Pinecone results for same category parts that are in stock
    2. Prefer aftermarket over nothing
    3. Return None if no alternative found

    This prevents the pipeline from failing completely when
    one part is unavailable.
    """
    # Get original part details for category matching
    original = get_part_details(original_part_number)
    if not original:
        return None

    original_category = original.get("subcategory", "")

    # Search through Pinecone retrieved parts for same subcategory
    for candidate in retrieved_parts_context:
        candidate_number = candidate.get("part_number")

        # Skip the original unavailable part
        if candidate_number == original_part_number:
            continue

        # Check if same subcategory
        candidate_details = get_part_details(candidate_number)
        if not candidate_details:
            continue

        if candidate_details.get("subcategory") != original_category:
            continue

        # Check compatibility with vehicle
        is_compatible, _ = check_compatibility(candidate_details, vehicle)
        if not is_compatible:
            continue

        # Check stock
        stock = check_stock(candidate_number)
        if stock.get("qty_available", 0) > 0:
            return {
                **candidate_details,
                "is_alternative": True,
                "original_part":  original_part_number,
            }

    return None


# ── MAIN AGENT FUNCTION ───────────────────────────────────────────────────────

def run_inventory_agent(state: dict) -> dict:
    """
    Main Inventory Agent function — called by LangGraph orchestrator.

    Processes each required part:
    - Validates compatibility with vehicle
    - Checks available stock
    - Reserves if available
    - Finds alternative if not available
    - Flags for reorder if below threshold
    """
    start_time = time.time()
    ro_id = state.get("ro_id", "UNKNOWN")

    log_agent_start(
        agent_name="inventory_agent",
        ro_id=ro_id,
        input_summary={
            "required_parts": state.get("required_parts", []),
            "vehicle":        state.get("vehicle_details", {}).get("make"),
        }
    )

    try:

        required_parts          = state.get("required_parts", [])
        vehicle                 = state.get("vehicle_details", {})
        retrieved_parts_context = state.get("retrieved_parts_context", [])
        recommended_labor_codes = state.get("recommended_labor_codes", [])

        # Validate we have parts to check
        if not required_parts:
            # Routine service with no specific parts identified
            # Still valid — quoting agent handles labor-only ROs
            log_agent_end(
                agent_name="inventory_agent",
                ro_id=ro_id,
                output_summary={"note": "No specific parts required"},
                latency_ms=int((time.time() - start_time) * 1000),
            )
            return {
                **state,
                "inventory_check":  {},
                "parts_available":  True,
                "reserved_parts":   [],
                "unavailable_parts": [],
                "reorder_needed":   [],
                "current_agent":    "inventory_agent",
                "error":            None,
            }

        # ── PROCESS EACH REQUIRED PART ─────────────────────────────────────
        reserved_parts    = []
        unavailable_parts = []
        reorder_needed    = []
        inventory_check   = {}

        for part_number in required_parts:

            part_result = {
                "part_number":      part_number,
                "status":           None,
                "qty_requested":    1,  # default 1 unit per part
                "qty_reserved":     0,
                "compatible":       False,
                "compatibility_reason": None,
                "stock_before":     0,
                "available_before": 0,
                "alternative_part": None,
                "unit_cost":        0,
                "sell_price":       0,
                "bin_location":     None,
                "brand":            None,
                "description":      None,
            }

            # Step A — Fetch part details from PostgreSQL
            part_details = get_part_details(part_number)

            if not part_details:
                part_result["status"] = "NOT_FOUND"
                part_result["compatible"] = False
                unavailable_parts.append(part_number)
                inventory_check[part_number] = part_result
                continue

            # Populate part details in result
            part_result["unit_cost"]    = part_details["unit_cost"]
            part_result["sell_price"]   = part_details["sell_price"]
            part_result["bin_location"] = part_details["bin_location"]
            part_result["brand"]        = part_details["brand"]
            part_result["description"]  = part_details["description"]

            # Step B — Validate compatibility with vehicle
            is_compatible, compatibility_reason = check_compatibility(
                part_details, vehicle
            )

            part_result["compatible"]           = is_compatible
            part_result["compatibility_reason"] = compatibility_reason

            if not is_compatible:
                # Part exists but wrong vehicle
                # Try to find compatible alternative
                alternative = find_alternative_part(
                    original_part_number=part_number,
                    vehicle=vehicle,
                    retrieved_parts_context=retrieved_parts_context,
                )

                if alternative:
                    part_result["status"]           = "INCOMPATIBLE_ALTERNATIVE_FOUND"
                    part_result["alternative_part"] = alternative
                else:
                    part_result["status"] = "INCOMPATIBLE_NO_ALTERNATIVE"
                    unavailable_parts.append(part_number)

                inventory_check[part_number] = part_result
                continue

            # Step C — Check current stock levels
            stock = check_stock(part_number)

            part_result["stock_before"]   = stock["qty_on_hand"]
            part_result["available_before"] = stock["qty_available"]

            if stock["qty_available"] <= 0:
                # Out of stock — look for alternative
                alternative = find_alternative_part(
                    original_part_number=part_number,
                    vehicle=vehicle,
                    retrieved_parts_context=retrieved_parts_context,
                )

                if alternative:
                    part_result["status"]           = "OUT_OF_STOCK_ALTERNATIVE_FOUND"
                    part_result["alternative_part"] = alternative
                else:
                    part_result["status"] = "OUT_OF_STOCK_NO_ALTERNATIVE"
                    unavailable_parts.append(part_number)

                inventory_check[part_number] = part_result
                continue

            # Step D — Reserve the part
            success, message = reserve_parts(
                part_number=part_number,
                quantity=1,
                ro_id=ro_id,
            )

            if success:
                part_result["status"]       = "RESERVED"
                part_result["qty_reserved"] = 1

                reserved_parts.append({
                    "part_number":  part_number,
                    "description":  part_details["description"],
                    "brand":        part_details["brand"],
                    "qty_reserved": 1,
                    "unit_cost":    part_details["unit_cost"],
                    "sell_price":   part_details["sell_price"],
                    "bin_location": part_details["bin_location"],
                    "oem_part_number": part_details.get("oem_part_number"),
                })

                # Step E — Flag for reorder if below threshold
                if check_reorder_needed(part_number):
                    reorder_needed.append(part_number)

            else:
                # Reservation failed (race condition or other error)
                part_result["status"] = "RESERVATION_FAILED"
                unavailable_parts.append(part_number)

            inventory_check[part_number] = part_result

        # ── DETERMINE OVERALL AVAILABILITY ─────────────────────────────────
        # parts_available = True only if ALL required parts are reserved
        # Even one unavailable part triggers Replenishment Agent
        parts_available = len(unavailable_parts) == 0

        # ── GUARDRAIL VALIDATION ───────────────────────────────────────────
        output = {
            "inventory_check":   inventory_check,
            "parts_available":   parts_available,
            "reserved_parts":    reserved_parts,
            "unavailable_parts": unavailable_parts,
            "reorder_needed":    reorder_needed,
        }

        is_valid, reason = validate_inventory_output(output, ro_id)

        if not is_valid:
            log_guardrail_failure("inventory_agent", ro_id, reason)

        # ── LOG AND RETURN ─────────────────────────────────────────────────
        latency_ms = int((time.time() - start_time) * 1000)

        log_agent_end(
            agent_name="inventory_agent",
            ro_id=ro_id,
            output_summary={
                "total_parts":      len(required_parts),
                "reserved":         len(reserved_parts),
                "unavailable":      len(unavailable_parts),
                "reorder_needed":   len(reorder_needed),
                "parts_available":  parts_available,
                "latency_ms":       latency_ms,
            },
            latency_ms=latency_ms,
        )

        return {
            **state,
            "inventory_check":   inventory_check,
            "parts_available":   parts_available,
            "reserved_parts":    reserved_parts,
            "unavailable_parts": unavailable_parts,
            "reorder_needed":    reorder_needed,
            "current_agent":     "inventory_agent",
            "error":             None,
        }

    except Exception:
        latency_ms = int((time.time() - start_time) * 1000)

        log_agent_error(
            agent_name="inventory_agent",
            ro_id=ro_id,
            error=str(e),
            input_state={"required_parts": state.get("required_parts", [])},
        )

        return {
            **state,
            "error":         str(e),
            "current_agent": "inventory_agent",
        }


# ── STANDALONE TEST ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))

    # Simulate state coming from Intake Agent
    test_state = {
        "ro_id": "RO-TEST-002",
        "required_parts": [
            "BRK-PAD-HON-F-01",
            "BRK-ROT-HON-F-01",
            "FLT-OIL-HON-01",
        ],
        "vehicle_details": {
            "vin":       "TEST-VIN-001",
            "make":      "Honda",
            "model":     "City",
            "year":      2021,
            "fuel_type": "Diesel",
            "is_ev":     False,
        },
        "retrieved_parts_context": [],
        "recommended_labor_codes": ["BRK-001", "BRK-002"],
        "fault_classification":    "BRAKE_SYSTEM",
        "urgency":                 "HIGH",
    }

    print("\nRunning Inventory Agent test...")
    print(f"Parts to check: {test_state['required_parts']}\n")

    result = run_inventory_agent(test_state)

    if result.get("error"):
        print(f"ERROR: {result['error']}")
    else:
        print(f"Parts Available:   {result['parts_available']}")
        print(f"Reserved Parts:    {len(result['reserved_parts'])}")
        print(f"Unavailable Parts: {result['unavailable_parts']}")
        print(f"Reorder Needed:    {result['reorder_needed']}")

        print("\nDetailed Results:")
        for part_num, details in result["inventory_check"].items():
            print(
                f"  {part_num}: "
                f"status={details['status']} | "
                f"compatible={details['compatible']} | "
                f"stock_before={details['stock_before']} | "
                f"reserved={details['qty_reserved']}"
            )