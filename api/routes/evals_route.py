"""
CONDUIT — Evals API Route
==========================
Serves eval results from evals/reports/latest/
so you can check system health without SSH-ing into EC2.

Endpoints:
    GET /evals/summary          JSON summary of latest run
    GET /evals/status           Simple pass/fail + timestamp (for monitoring)
    GET /evals/badge            Shield.io compatible badge data
"""

import os
import json
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/evals", tags=["Evals"])

REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "evals", "reports", "latest"
)
SUMMARY_PATH = os.path.join(REPORTS_DIR, "summary.json")


def _load_summary() -> dict:
    """Loads summary.json — raises 404 if evals haven't been run yet."""
    if not os.path.exists(SUMMARY_PATH):
        raise HTTPException(
            status_code=404,
            detail="No eval results found. Run: python evals/run_evals.py --free"
        )
    with open(SUMMARY_PATH) as f:
        return json.load(f)


@router.get("/summary")
def get_eval_summary():
    """
    Full eval summary — all modules, pass rates, timing.

    Returns the complete summary.json from the latest eval run.
    Run `python evals/run_evals.py --free` to generate/refresh.
    """
    summary = _load_summary()
    return JSONResponse(content=summary)


@router.get("/status")
def get_eval_status():
    """
    Quick health check — did the last eval run pass?

    Lighter than /summary — designed for monitoring/uptime checks.
    Returns 200 if all passed, 500 if any failed.
    """
    summary = _load_summary()

    status = {
        "all_passed":    summary.get("all_passed", False),
        "passed":        summary.get("passed", 0),
        "total":         summary.get("total", 0),
        "run_at":        summary.get("run_at"),
        "total_elapsed": summary.get("total_elapsed"),
    }

    if not status["all_passed"]:
        failed = [
            m["label"] for m in summary.get("modules", [])
            if not m.get("passed")
        ]
        status["failed_modules"] = failed
        return JSONResponse(status_code=500, content=status)

    return JSONResponse(content=status)


@router.get("/badge")
def get_eval_badge():
    """
    Shield.io compatible badge data.
    Use in README: https://img.shields.io/endpoint?url=YOUR_EC2_URL/evals/badge
    """
    try:
        summary = _load_summary()
        passed  = summary.get("passed", 0)
        total   = summary.get("total", 0)
        all_ok  = summary.get("all_passed", False)

        return {
            "schemaVersion": 1,
            "label":         "evals",
            "message":       f"{passed}/{total} passing",
            "color":         "brightgreen" if all_ok else "red",
            "namedLogo":     "checkmarx",
        }
    except HTTPException:
        return {
            "schemaVersion": 1,
            "label":         "evals",
            "message":       "not run",
            "color":         "lightgrey",
        }