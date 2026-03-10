"""
CONDUIT — Quoting Agent (Agent 3)
===================================
Receives reserved parts from Inventory Agent.
Generates OEM and aftermarket quotes with full line items.

Responsibilities:
    1. Fetch labor operations for this job
    2. Build OEM quote (genuine parts pricing)
    3. Build aftermarket quote (alternative pricing ~30% cheaper)
    4. Apply loyalty discount or recall coverage
    5. Calculate GST and final totals
    6. Determine if human approval is needed
    7. Save quote to PostgreSQL
    8. Return quote for Transaction Agent

Key design decision — NO LLM in this agent:
    All calculations are deterministic.
    Prices come from PostgreSQL pricing table.
    Math must be exact — a wrong quote loses customer trust.
    GPT-4o has no role in financial calculations.

Input state fields used:
    - ro_id
    - reserved_parts            (from Inventory Agent)
    - recommended_labor_codes   (from Intake Agent)
    - customer_details          (from Intake Agent)
    - vehicle_details           (from Intake Agent)
    - fault_classification      (from Intake Agent)
    - recall_action_required    (from Intake Agent)
    - is_ev_job                 (from Intake Agent)

Output state fields added:
    - quote             (full quote object)
    - quote_id          (saved to PostgreSQL)
    - requires_approval (bool)
    - approval_reason   (string)
    - error             (if something went wrong)
"""

import os
import sys
import uuid
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# ── PATH SETUP ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
)))

from dotenv import load_dotenv
load_dotenv()

from tools.pricing_tools import (
    get_labor_operations,
    infer_labor_from_parts,
    calculate_discount,
    build_parts_line_items,
    build_labor_line_items,
    calculate_totals,
    requires_approval,
)

from database.connection import get_session
from database.models import Quote, RepairOrder

from app_logging.agent_logger import (
    log_agent_start,
    log_agent_end,
    log_agent_error,
    log_guardrail_failure,
)


# ── GUARDRAILS ────────────────────────────────────────────────────────────────

def validate_quote_output(quote: dict, ro_id: str) -> tuple:
    """
    Validates quote before saving to DB and passing downstream.
    Returns (is_valid, reason)

    Financial accuracy is critical — wrong quotes lose customers
    and cause revenue leakage.
    """

    # 1. Total must be positive
    total = quote.get("total_amount", 0)
    if not isinstance(total, (int, float)) or total <= 0:
        return False, f"Invalid quote total: {total}"

    # 2. Line items must exist
    line_items = quote.get("line_items", [])
    if not line_items:
        return False, "Quote has no line items"

    # 3. Each line item must have required fields
    for item in line_items:
        for field in ["type", "description", "subtotal"]:
            if field not in item:
                return False, f"Line item missing field: {field}"

    # 4. Math validation — recalculate and verify
    calculated_subtotal = round(
        sum(item["subtotal"] for item in line_items), 2
    )
    stated_subtotal = quote.get("subtotal", 0)

    if abs(calculated_subtotal - stated_subtotal) > 1:
        return False, (
            "Quote arithmetic mismatch: "
            f"calculated {calculated_subtotal} "
            f"vs stated {stated_subtotal}"
        )

    # 5. Discount cannot exceed 30%
    discount_rate = quote.get("discount_rate", 0)
    if discount_rate > 0.30 and not quote.get("recall_action_required"):
        return False, (
            f"Discount {discount_rate:.0%} exceeds 30% maximum"
        )

    # 6. GST must be 18% of post-discount amount
    post_discount   = quote.get("post_discount", 0)
    stated_gst      = quote.get("gst_amount", 0)
    calculated_gst  = round(post_discount * 0.18, 2)

    if abs(stated_gst - calculated_gst) > 1:
        return False, (
            "GST calculation error: "
            f"calculated {calculated_gst} "
            f"vs stated {stated_gst}"
        )

    return True, "OK"


# ── SAVE QUOTE TO DATABASE ────────────────────────────────────────────────────

def save_quote_to_db(
    ro_id: str,
    oem_quote: dict,
    aftermarket_quote: dict,
    selected_quote: dict,
    needs_approval: bool,
) -> str:
    """
    Saves quote to PostgreSQL quotes table.
    Updates repair_order with quote_id.
    Returns quote_id.
    """
    quote_id    = f"QT-{str(uuid.uuid4())[:8].upper()}"
    valid_until = datetime.utcnow() + timedelta(days=3)

    try:
        with get_session() as db:

            # Save quote
            quote = Quote(
                quote_id          = quote_id,
                ro_id             = ro_id,
                line_items        = selected_quote.get("line_items", []),
                subtotal          = selected_quote.get("subtotal", 0),
                discount_amount   = selected_quote.get("discount_amount", 0),
                gst_amount        = selected_quote.get("gst_amount", 0),
                total_amount      = selected_quote.get("total_amount", 0),
                status            = "PENDING_APPROVAL" if needs_approval
                                    else "APPROVED",
                oem_quote         = oem_quote,
                aftermarket_quote = aftermarket_quote,
                requires_approval = needs_approval,
                valid_until       = valid_until,
            )
            db.add(quote)

            # Update repair order with quote reference
            ro = db.query(RepairOrder).filter(
                RepairOrder.ro_id == ro_id
            ).first()
            if ro:
                ro.quote_id = quote_id
                ro.status   = "QUOTED"

            db.commit()

        return quote_id

    except Exception:
        raise Exception(f"Failed to save quote: {e}")


# ── MAIN AGENT FUNCTION ───────────────────────────────────────────────────────

def run_quoting_agent(state: dict) -> dict:
    """
    Main Quoting Agent function — called by LangGraph orchestrator.

    Builds two quotes (OEM + aftermarket) and saves to PostgreSQL.
    No LLM calls — pure deterministic calculation.
    """
    start_time = time.time()
    ro_id      = state.get("ro_id", "UNKNOWN")

    log_agent_start(
        agent_name="quoting_agent",
        ro_id=ro_id,
        input_summary={
            "reserved_parts": len(state.get("reserved_parts", [])),
            "customer_tier":  state.get("customer_details", {}).get(
                                  "loyalty_tier_name", "Walk-in"
                              ),
            "fault":          state.get("fault_classification"),
        }
    )

    try:

        reserved_parts          = state.get("reserved_parts", [])
        recommended_labor_codes = state.get("recommended_labor_codes", [])
        customer_details        = state.get("customer_details")
        vehicle_details         = state.get("vehicle_details", {})
        fault_classification    = state.get("fault_classification")
        recall_action_required  = state.get("recall_action_required", False)
        is_ev_job               = state.get("is_ev_job", False)

        # ── STEP 1: GET LABOR OPERATIONS ───────────────────────────────────
        if recommended_labor_codes:
            labor_operations = get_labor_operations(recommended_labor_codes)
        else:
            # Infer from parts if Intake Agent didn't provide labor codes
            labor_operations = infer_labor_from_parts(reserved_parts)

        # ── STEP 2: CALCULATE DISCOUNT ─────────────────────────────────────
        discount_rate, discount_reason = calculate_discount(
            subtotal               = 0,  # calculated after line items
            customer_details       = customer_details,
            fault_classification   = fault_classification,
            recall_action_required = recall_action_required,
        )

        # ── STEP 3: BUILD OEM QUOTE ────────────────────────────────────────
        oem_parts_items  = build_parts_line_items(reserved_parts, use_oem=True)
        labor_items      = build_labor_line_items(labor_operations)
        oem_line_items   = oem_parts_items + labor_items
        oem_totals       = calculate_totals(oem_line_items, discount_rate)

        oem_quote = {
            "quote_type":       "OEM",
            "line_items":       oem_line_items,
            "parts_count":      len(oem_parts_items),
            "labor_count":      len(labor_items),
            "discount_reason":  discount_reason,
            **oem_totals,
        }

        # ── STEP 4: BUILD AFTERMARKET QUOTE ───────────────────────────────
        # Only build aftermarket if non-EV and non-recall job
        # EV parts must always be genuine
        # Recall parts must always be OEM (manufacturer requirement)
        aftermarket_quote = None

        if not is_ev_job and not recall_action_required:
            am_parts_items     = build_parts_line_items(
                                     reserved_parts, use_oem=False
                                 )
            am_line_items      = am_parts_items + labor_items
            am_totals          = calculate_totals(am_line_items, discount_rate)

            aftermarket_quote = {
                "quote_type":      "AFTERMARKET",
                "line_items":      am_line_items,
                "parts_count":     len(am_parts_items),
                "labor_count":     len(labor_items),
                "discount_reason": discount_reason,
                **am_totals,
            }

        # ── STEP 5: SELECT DEFAULT QUOTE ──────────────────────────────────
        # Default to OEM — customer can choose aftermarket in UI
        selected_quote = oem_quote

        # ── STEP 6: APPROVAL CHECK ─────────────────────────────────────────
        needs_approval, approval_reason = requires_approval(
            total_amount = selected_quote["total_amount"],
            is_ev_job    = is_ev_job,
        )

        # ── STEP 7: GUARDRAIL VALIDATION ──────────────────────────────────
        is_valid, reason = validate_quote_output(selected_quote, ro_id)

        if not is_valid:
            log_guardrail_failure("quoting_agent", ro_id, reason)
            raise ValueError(f"Quote validation failed: {reason}")

        # ── STEP 8: SAVE TO DATABASE ───────────────────────────────────────
        quote_id = save_quote_to_db(
            ro_id             = ro_id,
            oem_quote         = oem_quote,
            aftermarket_quote = aftermarket_quote,
            selected_quote    = selected_quote,
            needs_approval    = needs_approval,
        )

        # ── STEP 9: BUILD OUTPUT STATE ─────────────────────────────────────
        latency_ms = int((time.time() - start_time) * 1000)

        log_agent_end(
            agent_name="quoting_agent",
            ro_id=ro_id,
            output_summary={
                "quote_id":        quote_id,
                "total_amount":    selected_quote["total_amount"],
                "discount_rate":   discount_rate,
                "needs_approval":  needs_approval,
                "latency_ms":      latency_ms,
            },
            latency_ms=latency_ms,
        )

        return {
            **state,
            "quote":            selected_quote,
            "quote_id":         quote_id,
            "oem_quote":        oem_quote,
            "aftermarket_quote": aftermarket_quote,
            "requires_approval": needs_approval,
            "approval_reason":  approval_reason,
            "discount_rate":    discount_rate,
            "discount_reason":  discount_reason,
            "current_agent":    "quoting_agent",
            "error":            None,
        }

    except Exception:
        latency_ms = int((time.time() - start_time) * 1000)

        log_agent_error(
            agent_name="quoting_agent",
            ro_id=ro_id,
            error=str(e),
            input_state={
                "reserved_parts": len(state.get("reserved_parts", [])),
            },
        )

        return {
            **state,
            "error":         str(e),
            "current_agent": "quoting_agent",
        }


# ── STANDALONE TEST ───────────────────────────────────────────────────────────

if __name__ == "__main__":

    # Simulate state coming from Inventory Agent
    test_state = {
        "ro_id": "RO-TEST-003",
        "reserved_parts": [
            {
                "part_number":   "BRK-PAD-HON-F-01",
                "description":   "Front disc brake pads Honda City 2019-2023",
                "brand":         "Honda Genuine",
                "qty_reserved":  1,
                "unit_cost":     3850.0,
                "sell_price":    5200.0,
                "bin_location":  "A-14",
                "oem_part_number": "45022-T9A-H01",
            },
            {
                "part_number":   "BRK-ROT-HON-F-01",
                "description":   "Front brake disc rotor Honda City 280mm",
                "brand":         "Honda Genuine",
                "qty_reserved":  1,
                "unit_cost":     4200.0,
                "sell_price":    5800.0,
                "bin_location":  "B-07",
                "oem_part_number": "45251-T9A-000",
            },
        ],
        "recommended_labor_codes": ["BRK-003"],
        "customer_details": {
            "customer_id":       "CUST-ABC123",
            "full_name":         "Rahul Sharma",
            "loyalty_tier":      2,
            "loyalty_tier_name": "Silver",
            "discount_rate":     0.08,
        },
        "vehicle_details": {
            "make": "Honda", "model": "City",
            "year": 2021, "fuel_type": "Diesel",
            "is_ev": False,
        },
        "fault_classification":   "BRAKE_SYSTEM",
        "recall_action_required": False,
        "is_ev_job":              False,
    }

    print("\nRunning Quoting Agent test...")
    print(f"Parts: {[p['part_number'] for p in test_state['reserved_parts']]}")
    print(f"Customer: {test_state['customer_details']['full_name']} "
          f"({test_state['customer_details']['loyalty_tier_name']})\n")

    result = run_quoting_agent(test_state)

    if result.get("error"):
        print(f"ERROR: {result['error']}")
    else:
        quote = result["quote"]
        print(f"Quote ID:          {result['quote_id']}")
        print(f"Requires Approval: {result['requires_approval']}")
        print(f"\n{'─'*45}")
        print(f"{'CONDUIT QUOTE':^45}")
        print(f"{'─'*45}")

        for item in quote["line_items"]:
            if item["type"] == "PART":
                print(f"  {item['description'][:35]:<35} "
                      f"₹{item['subtotal']:>8,.0f}")
            else:
                print(f"  {item['description'][:35]:<35} "
                      f"₹{item['subtotal']:>8,.0f}")

        print(f"{'─'*45}")
        print(f"  {'Subtotal':<35} ₹{quote['subtotal']:>8,.0f}")
        print(f"  {'Discount (' + result['discount_reason'] + ')':<35} "
              f"-₹{quote['discount_amount']:>7,.0f}")
        print(f"  {'GST (18%)':<35} ₹{quote['gst_amount']:>8,.0f}")
        print(f"{'─'*45}")
        print(f"  {'TOTAL':<35} ₹{quote['total_amount']:>8,.0f}")
        print(f"{'─'*45}")

        if result.get("aftermarket_quote"):
            am = result["aftermarket_quote"]
            print(f"\n  Aftermarket Option:    ₹{am['total_amount']:>8,.0f}")
            saving = quote["total_amount"] - am["total_amount"]
            print(f"  Potential saving:      ₹{saving:>8,.0f}")