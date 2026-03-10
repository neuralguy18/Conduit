"""
CONDUIT — FastAPI Backend
==========================
Main application entry point.

Endpoints:
    POST /api/repair-orders/          → create RO + run pipeline
    GET  /api/repair-orders/          → list all ROs
    GET  /api/repair-orders/{ro_id}   → get single RO
    POST /api/repair-orders/{ro_id}/approve  → HITL approve (transaction)
    POST /api/repair-orders/{ro_id}/intake-review → HITL intake override

    GET  /api/inventory/              → list all parts
    GET  /api/inventory/{part_number} → get single part

    GET  /api/quotes/{quote_id}       → get quote details

    GET  /api/purchase-orders/        → list all POs
    GET  /api/dashboard/stats         → summary stats for dashboard

    GET  /health                      → health check

Run:
    uvicorn api.main:app --reload --port 8000
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app_logging.log_middleware import LoggingMiddleware
from app_logging.logger import get_logger
from database.connection import check_db_connection, get_table_counts
from config import validate_required_config, ENVIRONMENT

from api.routes import repair_orders, inventory, quotes, purchase_orders, dashboard, evals_route as evals

logger = get_logger("conduit.api")

# ── APP INIT ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "CONDUIT API",
    description = "Multi-agent automotive service intelligence platform",
    version     = "1.0.0",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

# ── MIDDLEWARE ────────────────────────────────────────────────────────────────

# CORS — allow Streamlit dashboard to call API
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],  # tighten in production
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# Request/response logging
app.add_middleware(LoggingMiddleware)

# ── ROUTERS ───────────────────────────────────────────────────────────────────

app.include_router(repair_orders.router,   prefix="/api")
app.include_router(inventory.router,       prefix="/api")
app.include_router(quotes.router,          prefix="/api")
app.include_router(purchase_orders.router, prefix="/api")
app.include_router(dashboard.router,       prefix="/api")
app.include_router(evals.router,           prefix="/api")

# ── HEALTH CHECK ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
def health_check():
    """
    Health check endpoint.
    Called by Docker healthcheck and load balancer.
    """
    try:
        check_db_connection()
        counts = get_table_counts()
        return {
            "status":      "healthy",
            "environment": ENVIRONMENT,
            "database":    "connected",
            "tables":      counts,
        }
    except Exception:
        return {
            "status":   "unhealthy",
            "database": "disconnected",
            "error":    str(e),
        }


# ── STARTUP EVENT ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Validates config and DB connection on startup."""
    logger.info({
        "event":       "api_startup",
        "environment": ENVIRONMENT,
        "message":     "CONDUIT API starting up",
    })

    try:
        validate_required_config()
        check_db_connection()
        counts = get_table_counts()

        logger.info({
            "event":   "startup_complete",
            "message": "All systems ready",
            "tables":  counts,
        })

    except Exception as e:
        logger.error({
            "event":   "startup_failed",
            "error":   str(e),
        })
        raise