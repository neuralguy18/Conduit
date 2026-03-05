"""
CONDUIT — Transaction Agent (Agent 4)
=======================================
Finalizes the repair order after quote is generated.

Responsibilities:
    1. Check if human approval is needed (HITL)
    2. If HITL enabled — pause and wait for advisor decision
    3. If HITL disabled — auto approve within threshold
    4. On approval — confirm parts reservations
    5. Update RO status to IN_PROGRESS
    6. Trigger replenishment check for low stock parts
    7. Close the transaction loop

HITL behaviour controlled by HITL_ENABLED in .env:
    HITL_ENABLED=false → instant auto-approval (portfolio/CV deployment)
    HITL_ENABLED=true  → pauses for human approval (live demo / production)
"""

import os
import sys
import time
from datetime import datetime
from typing import Optional

# ── PATH SETUP ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
)))

from dotenv import load_dotenv
load_dotenv()

from database.connection import get_session
from database.models import RepairOrder, Quote, Inventory

from app_logging.agent_logger import (
    log_agent_start,
    log_agent_end,
    log_agent_error,
    log_guardrail_failure,
)

# ── CONFIG ────────────────────────────────────────────────────────────────────

HITL_ENABLED           = os.getenv("HITL_ENABLED", "false").lower() == "true"
AUTO_APPROVE_THRESHOLD = float(os.getenv("AUTO_APPROVE_THRESHOLD", "50000"))


# ── HITL ROUTING ──────────────────────────────────────────────────────────────

def should_pause_for_human(state: dict) -> bool:
    """
    Determines if this RO needs human approval before transaction.

    When HITL_ENABLED=false — always returns False
    (instant pipeline for portfolio/CV deployment)

    When HITL_ENABLED=true — pauses for:
    - EV jobs (safety + high cost)
    - Quotes above AUTO_APPROVE_THRESHOLD
    - Low confidence classifications
    - Active recall jobs
    """
    if not HITL_ENABLED:
        return False

    # EV jobs always need approval
    if state.get("is_ev_job"):
        return True

    # High value quotes
    quote_total = state.get("quote", {}).get("total_amount", 0)
    if quote_total > AUTO_APPROVE_THRESHOLD:
        return True

    # Low confidence from Intake Agent
    if state.get("intake_confidence", 1.0) < 0.70:
        return True

    # Recall jobs
    if state.get("recall_action_required"):
        return True

    return False


def get_auto_approval_reason(state: dict) -> str:
    """Returns reason string for auto-approval decision."""
    if not HITL_ENABLED:
        return "HITL disabled — auto-approved for portfolio deployment"

    quote_total = state.get("quote", {}).get("total_amount", 0)
    return (
        "Within auto-approve threshold "
        f"(₹{quote_total:,.0f} < ₹{AUTO_APPROVE_THRESHOLD:,.0f})"
    )


# ── HITL INTERRUPT (LangGraph) ────────────────────────────────────────────────

def run_human_review(state: dict) -> dict:
    """
    Pauses pipeline for human approval using LangGraph interrupt.
    Only called when HITL_ENABLED=true AND conditions are met.

    The pipeline freezes here until the Streamlit dashboard
    calls graph.invoke() with the advisor's decision.
    State is preserved in PostgreSQL checkpoint between pause and resume.
    """
    try:
        from langgraph.types import interrupt

        quote   = state.get("quote", {})
        ro_id   = state.get("ro_id")

        # This line PAUSES the pipeline — execution stops here
        # Resumes when graph.invoke() is called from dashboard
        decision = interrupt({
            "ro_id":           ro_id,
            "message":         "Quote requires human approval before proceeding",
            "quote_total":     quote.get("total_amount"),
            "approval_reason": state.get("approval_reason"),
            "is_ev_job":       state.get("is_ev_job"),
            "fault":           state.get("fault_classification"),
            "vehicle":         (
                f"{state.get('vehicle_details', {}).get('year')} "
                f"{state.get('vehicle_details', {}).get('make')} "
                f"{state.get('vehicle_details', {}).get('model')}"
            ),
        })

        # After resume() is called from dashboard
        approved     = decision.get("approved", False)
        advisor_id   = decision.get("advisor_id", "UNKNOWN")
        notes        = decision.get("notes", "")

        if approved:
            return {
                **state,
                "human_approved":   True,
                "approved_by":      advisor_id,
                "approval_notes":   notes,
                "approval_method":  "HUMAN_REVIEW",
            }
        else:
            return {
                **state,
                "human_approved":   False,
                "rejection_reason": decision.get("reason", "Rejected by advisor"),
                "approval_method":  "HUMAN_REVIEW",
            }

    except ImportError:
        # LangGraph not available in standalone test mode
        # Default to auto-approve
        return {
            **state,
            "human_approved":  True,
            "approved_by":     "AUTO_FALLBACK",
            "approval_method": "AUTO_FALLBACK",
        }


# ── CONFIRM RESERVATIONS ──────────────────────────────────────────────────────

def confirm_parts_reservations(
    reserved_parts: list,
    ro_id: str,
) -> bool:
    """
    Converts soft reservations to confirmed allocations.
    Decrements qty_on_hand — parts physically allocated to this RO.

    Called only AFTER human approval (or auto-approval).
    This is the point of no return for inventory.
    """
    try:
        with get_session() as db:
            for part in reserved_parts:
                part_number  = part["part_number"]
                qty_reserved = part.get("qty_reserved", 1)

                inv = db.query(Inventory).filter(
                    Inventory.part_number == part_number
                ).with_for_update().first()

                if inv:
                    # Decrement on-hand and reserved simultaneously
                    inv.qty_on_hand  = max(0, inv.qty_on_hand - qty_reserved)
                    inv.qty_reserved = max(0, inv.qty_reserved - qty_reserved)

                    # Recalculate stock status
                    available = inv.qty_on_hand - inv.qty_reserved
                    if available <= 0:
                        inv.stock_status = "critical"
                    elif available <= inv.reorder_point:
                        inv.stock_status = "low"
                    else:
                        inv.stock_status = "healthy"

            db.commit()
        return True

    except Exception:
        return False


# ── UPDATE RO STATUS ──────────────────────────────────────────────────────────

def update_ro_status(
    ro_id: str,
    status: str,
    approved_by: Optional[str] = None,
    rejection_reason: Optional[str] = None,
) -> bool:
    """Updates repair order status in PostgreSQL."""
    try:
        with get_session() as db:
            ro = db.query(RepairOrder).filter(
                RepairOrder.ro_id == ro_id
            ).first()

            if ro:
                ro.status     = status
                ro.updated_at = datetime.utcnow()

                if status == "IN_PROGRESS":
                    ro.opened_at = ro.opened_at or datetime.utcnow()

                db.commit()
        return True

    except Exception:
        return False


def update_quote_status(
    quote_id: str,
    status: str,
    approved_by: Optional[str] = None,
) -> bool:
    """Updates quote status and approval details."""
    try:
        with get_session() as db:
            quote = db.query(Quote).filter(
                Quote.quote_id == quote_id
            ).first()

            if quote:
                quote.status = status
                if approved_by:
                    quote.approved_by = approved_by
                    quote.approved_at = datetime.utcnow()

                db.commit()
        return True

    except Exception:
        return False


# ── GUARDRAILS ────────────────────────────────────────────────────────────────

def validate_transaction_output(state: dict) -> tuple:
    """
    Final safety check before transaction is confirmed.
    Returns (is_valid, reason)
    """
    # Must have a quote
    if not state.get("quote_id"):
        return False, "No quote_id — cannot process transaction"

    # Must have reserved parts or be labor-only
    reserved = state.get("reserved_parts", [])
    fault    = state.get("fault_classification", "")

    if not reserved and fault not in ["ROUTINE_SERVICE"]:
        return False, "No reserved parts for non-routine job"

    # Quote must exist with valid total
    quote_total = state.get("quote", {}).get("total_amount", 0)
    if quote_total <= 0:
        return False, f"Invalid quote total: {quote_total}"

    return True, "OK"


# ── MAIN AGENT FUNCTION ───────────────────────────────────────────────────────

def run_transaction_agent(state: dict) -> dict:
    """
    Main Transaction Agent function.

    Flow:
    1. Validate state is ready for transaction
    2. Check if HITL needed
    3. If HITL → pause for human decision
    4. If auto → approve within threshold
    5. On approval → confirm reservations + update RO
    6. On rejection → release reservations + update RO
    7. Flag parts needing reorder for Replenishment Agent
    """
    start_time = time.time()
    ro_id      = state.get("ro_id", "UNKNOWN")

    log_agent_start(
        agent_name="transaction_agent",
        ro_id=ro_id,
        input_summary={
            "quote_total":  state.get("quote", {}).get("total_amount"),
            "hitl_enabled": HITL_ENABLED,
            "is_ev_job":    state.get("is_ev_job"),
        }
    )

    try:

        # ── STEP 1: VALIDATE INPUT STATE ──────────────────────────────────
        is_valid, reason = validate_transaction_output(state)
        if not is_valid:
            log_guardrail_failure("transaction_agent", ro_id, reason)
            raise ValueError(f"Transaction validation failed: {reason}")

        # ── STEP 2: DETERMINE APPROVAL PATH ───────────────────────────────
        needs_human = should_pause_for_human(state)

        if needs_human:
            # ── HITL PATH ─────────────────────────────────────────────────
            state = run_human_review(state)
            human_approved = state.get("human_approved", False)
            approved_by    = state.get("approved_by", "UNKNOWN")

        else:
            # ── AUTO-APPROVE PATH ─────────────────────────────────────────
            human_approved = True
            approved_by    = "AUTO_APPROVED"
            state = {
                **state,
                "human_approved":  True,
                "approved_by":     approved_by,
                "approval_method": "AUTO",
                "approval_reason": get_auto_approval_reason(state),
            }

        # ── STEP 3: PROCESS BASED ON APPROVAL DECISION ────────────────────
        if human_approved:

            # Confirm parts — point of no return
            reserved_parts = state.get("reserved_parts", [])
            confirm_parts_reservations(reserved_parts, ro_id)

            # Update RO to IN_PROGRESS
            update_ro_status(
                ro_id      = ro_id,
                status     = "IN_PROGRESS",
                approved_by = approved_by,
            )

            # Update quote to APPROVED
            update_quote_status(
                quote_id    = state.get("quote_id"),
                status      = "APPROVED",
                approved_by = approved_by,
            )

            transaction_status = "APPROVED"

        else:
            # Rejected — release all reservations
            from tools.inventory_tools import release_reservation

            for part in state.get("reserved_parts", []):
                release_reservation(
                    part_number = part["part_number"],
                    quantity    = part.get("qty_reserved", 1),
                    ro_id       = ro_id,
                )

            # Update RO to CANCELLED
            update_ro_status(
                ro_id             = ro_id,
                status            = "CANCELLED",
                rejection_reason  = state.get("rejection_reason"),
            )

            update_quote_status(
                quote_id = state.get("quote_id"),
                status   = "REJECTED",
            )

            transaction_status = "REJECTED"

        # ── STEP 4: LOG AND RETURN ─────────────────────────────────────────
        latency_ms = int((time.time() - start_time) * 1000)

        log_agent_end(
            agent_name="transaction_agent",
            ro_id=ro_id,
            output_summary={
                "transaction_status": transaction_status,
                "approved_by":        approved_by,
                "hitl_used":          needs_human,
                "latency_ms":         latency_ms,
            },
            latency_ms=latency_ms,
        )

        return {
            **state,
            "transaction_status": transaction_status,
            "approved_by":        approved_by,
            "hitl_triggered":     needs_human,
            "current_agent":      "transaction_agent",
            "error":              None,
        }

    except Exception:
        latency_ms = int((time.time() - start_time) * 1000)

        log_agent_error(
            agent_name="transaction_agent",
            ro_id=ro_id,
            error=str(e),
            input_state={
                "quote_id": state.get("quote_id"),
            },
        )

        return {
            **state,
            "error":         str(e),
            "current_agent": "transaction_agent",
        }


# ── STANDALONE TEST ───────────────────────────────────────────────────────────

if __name__ == "__main__":

    print(f"\nHITL_ENABLED = {HITL_ENABLED}")
    print(f"AUTO_APPROVE_THRESHOLD = ₹{AUTO_APPROVE_THRESHOLD:,.0f}\n")

    # Simulate state from Quoting Agent
    test_state = {
        "ro_id":    "RO-TEST-004",
        "quote_id": "QT-TEST001",
        "quote": {
            "total_amount":    15198.0,
            "subtotal":        14000.0,
            "discount_amount": 1120.0,
            "gst_amount":      2318.0,
            "line_items": [
                {
                    "type":        "PART",
                    "description": "Front disc brake pads Honda City",
                    "subtotal":    5200.0,
                },
                {
                    "type":        "LABOR",
                    "description": "Complete front brake service",
                    "subtotal":    3000.0,
                },
            ],
        },
        "reserved_parts": [
            {
                "part_number":  "BRK-PAD-HON-F-01",
                "qty_reserved": 1,
            },
        ],
        "fault_classification":   "BRAKE_SYSTEM",
        "recall_action_required": False,
        "is_ev_job":              False,
        "intake_confidence":      0.92,
        "vehicle_details": {
            "make": "Honda", "model": "City",
            "year": 2021,
        },
    }

    print("Running Transaction Agent test...")
    result = run_transaction_agent(test_state)

    if result.get("error"):
        print(f"ERROR: {result['error']}")
    else:
        print(f"Transaction Status: {result['transaction_status']}")
        print(f"Approved By:        {result['approved_by']}")
        print(f"HITL Triggered:     {result['hitl_triggered']}")
        print(f"Approval Method:    {result.get('approval_method')}")
        print(f"Approval Reason:    {result.get('approval_reason')}")