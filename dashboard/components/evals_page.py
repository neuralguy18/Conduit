"""
CONDUIT — Evals Dashboard Page
================================
Reads a STATIC snapshot — never triggers a run.
Zero cost to view, zero API calls.

Cost model:
    Viewing this page  →  $0.00 always (reads static JSON)
    Running evals      →  manual only, via CLI
    LangSmith          →  automatic, zero marginal cost

To refresh results after a new run:
    cp evals/reports/latest/summary.json docs/eval_results.json
    git add docs/eval_results.json && git commit -m "docs: update eval snapshot"
"""

import os
import json
import streamlit as st
from datetime import datetime

# ── Static snapshot — priority order ──────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SNAPSHOT_PATHS = [
    os.path.join(ROOT, "docs", "eval_results.json"),          # committed snapshot (AWS)
    os.path.join(ROOT, "evals", "reports", "latest", "summary.json"),  # local run
]

# ── Demo data — shown when no real snapshot exists ────────────────────────────
DEMO_SUMMARY = {
    "_demo": True,
    "run_at": "2026-03-10T10:30:00Z",
    "all_passed": True,
    "passed": 7,
    "failed": 0,
    "total": 7,
    "total_elapsed": 218.4,
    "modules": [
        {"label": "Intake Guardrails",      "passed": True,  "cost": "$0.00", "elapsed_s": 38.2},
        {"label": "Quoting Guardrails",     "passed": True,  "cost": "$0.00", "elapsed_s": 22.1},
        {"label": "Output Validators",      "passed": True,  "cost": "$0.00", "elapsed_s": 19.6},
        {"label": "Inventory Agent",        "passed": True,  "cost": "$0.00", "elapsed_s": 28.4},
        {"label": "Quoting Agent",          "passed": True,  "cost": "$0.00", "elapsed_s": 31.7},
        {"label": "Transaction Agent",      "passed": True,  "cost": "$0.00", "elapsed_s": 44.8},
        {"label": "Replenishment Agent",    "passed": True,  "cost": "$0.00", "elapsed_s": 33.6},
    ],
}


def _load_snapshot() -> dict:
    for path in SNAPSHOT_PATHS:
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
                data["_source"] = "real"
                return data
    # Fall back to demo data — safe for recruiter demos, clearly labelled
    return DEMO_SUMMARY


def _fmt_time(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 60 else f"{seconds/60:.1f}m"


def _fmt_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y · %H:%M UTC")
    except Exception:
        return iso


def render_evals():

    st.markdown("""
    <div class="page-eyebrow">SYSTEM HEALTH</div>
    <div class="page-title">Eval Results</div>
    <div class="page-heading">Static snapshot — zero cost to view, no LLM calls triggered</div>
    """, unsafe_allow_html=True)

    summary   = _load_snapshot()
    is_demo   = summary.get("_demo", False)
    is_real   = summary.get("_source") == "real"

    # ── Source badge ──────────────────────────────────────────────────────────
    if is_demo:
        st.info(
            "**Demo mode** — showing representative results. "
            "Run `python evals/run_evals.py --free` and commit the snapshot to show real results.",
            icon="🧪"
        )
    else:
        st.markdown("""
        <div style="display:inline-flex;align-items:center;gap:6px;
                    background:#f0fdf4;border:1px solid #bbf7d0;border-radius:20px;
                    padding:4px 14px;font-size:0.78rem;color:#15803d;margin-bottom:16px;">
            ✓ Live snapshot — committed results, $0.00 to view
        </div>
        """, unsafe_allow_html=True)

    # ── Data ──────────────────────────────────────────────────────────────────
    all_passed    = summary.get("all_passed", False)
    passed        = summary.get("passed", 0)
    total         = summary.get("total", 0)
    failed        = summary.get("failed", 0)
    total_elapsed = summary.get("total_elapsed", 0)
    run_at        = summary.get("run_at", "")
    modules       = summary.get("modules", [])

    # ── Status banner ─────────────────────────────────────────────────────────
    if all_passed:
        st.markdown(f"""
        <div style="background:#f0fdf4;border:1.5px solid #16a34a;border-radius:10px;
                    padding:18px 24px;margin-bottom:24px;display:flex;align-items:center;gap:16px;">
            <span style="font-size:2rem;">✅</span>
            <div>
                <div style="font-size:1.1rem;font-weight:600;color:#15803d;">
                    ALL EVALS PASSING — System is production ready
                </div>
                <div style="font-size:0.82rem;color:#166534;margin-top:4px;">
                    Last run: {_fmt_date(run_at)} · {_fmt_time(total_elapsed)}
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        failed_names = [m["label"] for m in modules if not m.get("passed")]
        st.markdown(f"""
        <div style="background:#fef2f2;border:1.5px solid #dc2626;border-radius:10px;
                    padding:18px 24px;margin-bottom:24px;display:flex;align-items:center;gap:16px;">
            <span style="font-size:2rem;">❌</span>
            <div>
                <div style="font-size:1.1rem;font-weight:600;color:#991b1b;">
                    {failed} MODULE(S) FAILING — {', '.join(failed_names)}
                </div>
                <div style="font-size:0.82rem;color:#7f1d1d;margin-top:4px;">
                    Last run: {_fmt_date(run_at)}
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    # ── Metrics ───────────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Modules Passed", f"{passed}/{total}")
    with c2:
        st.metric("Pass Rate", f"{round(passed/total*100) if total else 0}%")
    with c3:
        st.metric("Runtime", _fmt_time(total_elapsed))
    with c4:
        all_free = all(m.get("cost") == "$0.00" for m in modules)
        st.metric("Cost to Run", "$0.00" if all_free else "~$1.00",
                  delta="free tier" if all_free else "LLM judge")

    st.markdown("---")

    # ── Module cards ──────────────────────────────────────────────────────────
    st.markdown("#### Module Breakdown")

    guardrail_labels = {"Guardrail", "Validator"}
    llm_labels       = {"Pipeline", "RAG", "LLM"}

    guardrails = [m for m in modules if any(x in m["label"] for x in guardrail_labels)]
    llm_evals  = [m for m in modules if any(x in m["label"] for x in llm_labels)]
    components = [m for m in modules if m not in guardrails and m not in llm_evals]

    sections = [
        ("🛡️ Guardrails — deterministic, always $0.00",  guardrails),
        ("⚙️ Component Evals — logic accuracy, $0.00",   components),
        ("🤖 LLM Evals — quality metrics, ~$1.00",        llm_evals),
    ]

    for section_title, section_modules in sections:
        if not section_modules:
            continue
        st.markdown(f"**{section_title}**")
        for m in section_modules:
            ok     = m.get("passed", False)
            bg     = "#f0fdf4" if ok else "#fef2f2"
            border = "#16a34a" if ok else "#dc2626"
            sc     = "#15803d" if ok else "#991b1b"
            st.markdown(f"""
            <div style="background:{bg};border:1px solid {border};border-radius:8px;
                        padding:12px 18px;margin-bottom:8px;
                        display:flex;align-items:center;justify-content:space-between;">
                <div style="display:flex;align-items:center;gap:10px;">
                    <span>{"✅" if ok else "❌"}</span>
                    <div>
                        <div style="font-weight:600;font-size:0.92rem;color:#1c1917;">
                            {m["label"]}
                        </div>
                        <div style="font-size:0.75rem;color:#57534e;margin-top:2px;">
                            Cost: {m.get("cost","$0.00")} · Runtime: {m.get("elapsed_s",0)}s
                        </div>
                    </div>
                </div>
                <div style="font-weight:700;font-size:0.82rem;color:{sc};letter-spacing:0.05em;">
                    {"PASSED" if ok else "FAILED"}
                </div>
            </div>
            """, unsafe_allow_html=True)
        st.markdown("")

    st.markdown("---")

    # ── Cost model explainer ──────────────────────────────────────────────────
    st.markdown("#### 💡 Cost Model")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("""
        **This page** — $0.00 always
        Reads a static JSON file, zero API calls.

        **Free evals** `--free`
        Guardrails + component evals.
        Pure Python logic, no LLM. $0.00/run.
        """)
    with col2:
        st.markdown("""
        **Full eval suite** — ~$1.00
        LLM judge metrics + RAG + pipeline.
        Run manually before deploy/demo.

        **LangSmith** — always on, free
        Traces every pipeline run automatically.
        View at smith.langchain.com.
        """)

    # ── Refresh instructions ──────────────────────────────────────────────────
    with st.expander("🔄 How to update these results"):
        st.code("""
# Run free evals ($0.00)
python evals/run_evals.py --free

# Commit snapshot — page auto-updates after push
cp evals/reports/latest/summary.json docs/eval_results.json
git add docs/eval_results.json
git commit -m "docs: update eval results snapshot"
git push
        """, language="bash")

    with st.expander("📄 Raw JSON"):
        st.json({k: v for k, v in summary.items() if not k.startswith("_")})