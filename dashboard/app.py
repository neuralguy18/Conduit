"""
CONDUIT — Streamlit Dashboard
Run: streamlit run dashboard/app.py
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import requests
import streamlit as st

# ── PAGE CONFIG ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title            = "CONDUIT",
    page_icon             = "🔩",
    layout                = "wide",
    initial_sidebar_state = "expanded",
)

# ── SESSION STATE ─────────────────────────────────────────────────────────────

if "page" not in st.session_state:
    st.session_state.page = "Overview"

if "show_guide" not in st.session_state:
    st.session_state.show_guide = True   # show guide banner on first load

# ── GLOBAL STYLES ─────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

:root {
    --bg:        #f8f7f4;
    --surface:   #ffffff;
    --surface2:  #f1efe9;
    --border:    #e2ded6;
    --sidebar:   #1a1a24;
    --sidebar2:  #222230;
    --accent:    #e8601a;
    --accent2:   #2563eb;
    --success:   #16a34a;
    --warning:   #d97706;
    --danger:    #dc2626;
    --text:      #1c1917;
    --text2:     #57534e;
    --muted:     #a8a29e;
    --mono:      'IBM Plex Mono', monospace;
    --sans:      'IBM Plex Sans', sans-serif;
}

/* ── BASE ── */
html, body, [class*="css"],
.main, .block-container,
[data-testid="stAppViewContainer"],
[data-testid="stMain"], section.main {
    font-family: var(--sans) !important;
    background-color: var(--bg) !important;
    color: var(--text) !important;
}

#MainMenu, footer, header, .stDeployButton { display: none !important; }

/* ── SIDEBAR — keep dark ── */
[data-testid="stSidebar"] {
    background-color: var(--sidebar) !important;
    border-right: 1px solid #2a2a38 !important;
}

[data-testid="stSidebar"] > div:first-child {
    padding: 0 !important;
}

/* ── NAV BUTTONS ── */
[data-testid="stSidebar"] .stButton {
    width: 100% !important;
    margin: 0 !important;
    padding: 0 !important;
}

[data-testid="stSidebar"] .stButton > button {
    width: 100% !important;
    text-align: left !important;
    font-family: var(--mono) !important;
    font-size: 0.78rem !important;
    letter-spacing: 0.02em !important;
    font-weight: 400 !important;
    color: #8b8fa8 !important;
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    padding: 11px 22px !important;
    margin: 0 !important;
    cursor: pointer !important;
    transition: all 0.12s ease !important;
    justify-content: flex-start !important;
    box-shadow: none !important;
}

[data-testid="stSidebar"] .stButton > button:hover {
    color: #f0f0f0 !important;
    background: rgba(255,255,255,0.05) !important;
    border: none !important;
    box-shadow: none !important;
}

[data-testid="stSidebar"] .stButton > button:focus {
    box-shadow: none !important;
    border: none !important;
    outline: none !important;
}

/* Active nav item */
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    color: #f97316 !important;
    background: rgba(249, 115, 22, 0.12) !important;
    border-left: 2px solid #f97316 !important;
    padding-left: 20px !important;
    font-weight: 500 !important;
    box-shadow: none !important;
}

/* ── METRIC CARDS ── */
[data-testid="metric-container"] {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: 6px !important;
    padding: 1.1rem 1.2rem !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
}

[data-testid="stMetricLabel"] > div {
    font-family: var(--mono) !important;
    font-size: 0.65rem !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: var(--muted) !important;
}

[data-testid="stMetricValue"] {
    font-family: var(--mono) !important;
    font-size: 1.6rem !important;
    font-weight: 600 !important;
    color: var(--text) !important;
}

/* ── MAIN CONTENT BUTTONS ── */
.main .stButton > button {
    font-family: var(--mono) !important;
    font-size: 0.75rem !important;
    letter-spacing: 0.06em !important;
    text-transform: uppercase !important;
    background: var(--accent) !important;
    border: none !important;
    color: #fff !important;
    border-radius: 4px !important;
    padding: 0.55rem 1.5rem !important;
    transition: all 0.15s !important;
    box-shadow: 0 1px 4px rgba(232,96,26,0.3) !important;
}

.main .stButton > button:hover {
    background: #d4530f !important;
    box-shadow: 0 2px 8px rgba(232,96,26,0.4) !important;
}

/* ── INPUTS ── */
.stTextInput input, .stTextArea textarea {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: 4px !important;
    color: var(--text) !important;
    font-family: var(--sans) !important;
    font-size: 0.9rem !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important;
}

.stTextInput input:focus, .stTextArea textarea:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 2px rgba(232,96,26,0.15) !important;
}

.stTextInput label, .stTextArea label {
    font-family: var(--mono) !important;
    font-size: 0.72rem !important;
    letter-spacing: 0.05em !important;
    text-transform: uppercase !important;
    color: var(--text2) !important;
    font-weight: 500 !important;
}

/* Selectbox */
[data-testid="stSelectbox"] > div > div {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important;
    border-radius: 4px !important;
    color: var(--text) !important;
}

/* ── DATAFRAME ── */
[data-testid="stDataFrame"] {
    border: 1px solid var(--border) !important;
    border-radius: 6px !important;
    overflow: hidden !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
}

/* ── EXPANDER ── */
[data-testid="stExpander"] {
    border: 1px solid var(--border) !important;
    border-radius: 6px !important;
    background: var(--surface) !important;
    margin-bottom: 0.75rem !important;
}

/* ── DIVIDER ── */
hr {
    border: none !important;
    border-top: 1px solid var(--border) !important;
    margin: 1.5rem 0 !important;
}

/* ── CUSTOM CLASSES ── */
.section-header {
    font-family: var(--mono);
    font-size: 0.65rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.5rem;
    margin-bottom: 1.25rem;
}

.page-title {
    margin-bottom: 1.75rem;
}

.page-eyebrow {
    font-family: var(--mono);
    font-size: 0.65rem;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 4px;
}

.page-heading {
    font-family: var(--mono);
    font-size: 1.75rem;
    font-weight: 600;
    color: #1c1917;
    margin: 0;
    line-height: 1.2;
}

/* Force all h1 tags in main content to be dark */
.main h1, [data-testid="stMain"] h1 {
    color: #1c1917 !important;
    font-family: var(--mono) !important;
}

.quote-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1.5rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}

.quote-total {
    font-family: var(--mono);
    font-size: 1.8rem;
    font-weight: 600;
    color: var(--accent);
    margin: 0.4rem 0;
}

.alert {
    padding: 0.85rem 1rem;
    border-radius: 5px;
    font-family: var(--mono);
    font-size: 0.75rem;
    margin-bottom: 1rem;
    line-height: 1.6;
}
.alert-warning {
    background: #fffbeb;
    border: 1px solid #fcd34d;
    color: #92400e;
}
.alert-danger {
    background: #fff1f2;
    border: 1px solid #fca5a5;
    color: #991b1b;
}
.alert-success {
    background: #f0fdf4;
    border: 1px solid #86efac;
    color: #14532d;
}
.alert-info {
    background: #eff6ff;
    border: 1px solid #93c5fd;
    color: #1e3a5f;
}

.guide-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 5px;
    padding: 1rem 1.2rem;
    margin-bottom: 0.75rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}

.guide-step-num {
    font-family: var(--mono);
    font-size: 0.65rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--accent);
    font-weight: 600;
    margin-bottom: 3px;
}

.guide-step-title {
    font-family: var(--mono);
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 4px;
}

.guide-step-desc {
    font-family: var(--sans);
    font-size: 0.82rem;
    color: var(--text2);
    line-height: 1.5;
}

.info-pill {
    display: inline-block;
    font-family: var(--mono);
    font-size: 0.62rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 2px 8px;
    border-radius: 20px;
    font-weight: 500;
    margin-right: 4px;
    margin-bottom: 4px;
}
.pill-orange  { background:#fff3e0; color:#c2410c; border:1px solid #fed7aa; }
.pill-green   { background:#f0fdf4; color:#15803d; border:1px solid #bbf7d0; }
.pill-blue    { background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe; }
.pill-grey    { background:#f5f5f4; color:#57534e; border:1px solid #e7e5e4; }
</style>
""", unsafe_allow_html=True)


# ── SIDEBAR ───────────────────────────────────────────────────────────────────

NAV_ITEMS = [
    ("Overview",         "🏠", "Overview"),
    ("New Repair Order", "🔧", "New Repair Order"),
    ("Repair Orders",    "📋", "Repair Orders"),
    ("Inventory",        "📦", "Inventory"),
    ("Quotes",           "🧾", "Quotes"),
    ("Purchase Orders",  "🚚", "Purchase Orders"),
    ("Pending Approval", "⏳", "Pending Approval"),
    ("Evals",            "🧪", "Evals"),
]

with st.sidebar:

    st.markdown("""
    <div style="padding:1.75rem 22px 1.25rem;">
        <div style="font-family:'IBM Plex Mono',monospace;font-size:1.25rem;
                    font-weight:600;color:#f97316;letter-spacing:0.04em;">
            ◈ CONDUIT
        </div>
        <div style="font-family:'IBM Plex Mono',monospace;font-size:0.58rem;
                    color:#4a4a60;letter-spacing:0.2em;text-transform:uppercase;
                    margin-top:4px;">
            Service Intelligence
        </div>
    </div>
    <div style="height:1px;background:#2a2a38;margin:0 0 6px;"></div>
    """, unsafe_allow_html=True)

    for key, icon, label in NAV_ITEMS:
        is_active = st.session_state.page == key
        btn = st.button(
            f"{icon}  {label}",
            key  = f"nav_{key}",
            type = "primary" if is_active else "secondary",
            use_container_width = True,
        )
        if btn:
            st.session_state.page = key
            st.rerun()

    st.markdown('<div style="height:1px;background:#2a2a38;margin:10px 0;"></div>',
                unsafe_allow_html=True)

    # API status
    try:
        r = requests.get("http://localhost:8000/health", timeout=2)
        api_online = r.status_code == 200
    except Exception:
        api_online = False

    dot        = "●" if api_online else "○"
    dot_color  = "#22c55e" if api_online else "#ef4444"
    api_label  = "API ONLINE" if api_online else "API OFFLINE"

    st.markdown(f"""
    <div style="padding:0 22px 16px;font-family:'IBM Plex Mono',monospace;
                font-size:0.65rem;letter-spacing:0.1em;">
        <span style="color:{dot_color};">{dot}</span>
        <span style="color:#4a4a60;margin-left:6px;">{api_label}</span>
    </div>
    """, unsafe_allow_html=True)

    # Quick help in sidebar
    with st.expander("📖  Quick Guide", expanded=False):
        st.markdown("""
        <div style="font-family:'IBM Plex Sans',sans-serif;font-size:0.78rem;
                    color:#8b8fa8;line-height:1.7;padding:4px 0;">
            <b style="color:#f97316;">New RO</b> — enter VIN + complaint to run the full pipeline.<br><br>
            <b style="color:#f97316;">Pending Approval</b> — high-value or EV jobs pause here for sign-off. Default PIN: <code style="color:#f97316;">1234</code><br><br>
            <b style="color:#f97316;">Inventory</b> — critical stock shown in red.<br><br>
            <b style="color:#f97316;">Purchase Orders</b> — auto-raised when stock falls below reorder point.
        </div>
        """, unsafe_allow_html=True)


# ── ONBOARDING BANNER (shown once) ───────────────────────────────────────────

if st.session_state.show_guide and st.session_state.page == "Overview":
    st.markdown("""
    <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:6px;
                padding:1rem 1.25rem;margin-bottom:1.5rem;display:flex;
                align-items:flex-start;gap:1rem;">
        <div style="font-size:1.5rem;line-height:1;">💡</div>
        <div>
            <div style="font-family:'IBM Plex Mono',monospace;font-size:0.72rem;
                        font-weight:600;color:#92400e;letter-spacing:0.05em;
                        text-transform:uppercase;margin-bottom:4px;">
                Welcome to CONDUIT
            </div>
            <div style="font-family:'IBM Plex Sans',sans-serif;font-size:0.82rem;
                        color:#78350f;line-height:1.6;">
                CONDUIT is a multi-agent automotive service platform. It classifies faults,
                checks inventory, generates quotes and raises purchase orders — automatically.<br>
                <b>Start by clicking "New Repair Order"</b> in the left panel, entering a VIN
                and complaint, then watch the pipeline execute in real time.
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    if st.button("✕  Dismiss", key="dismiss_guide"):
        st.session_state.show_guide = False
        st.rerun()

# ── PAGE ROUTING ──────────────────────────────────────────────────────────────

page = st.session_state.page

if page == "Overview":
    from dashboard.components.overview import render_overview
    render_overview()

elif page == "New Repair Order":
    from dashboard.components.new_ro import render_new_ro
    render_new_ro()

elif page == "Repair Orders":
    from dashboard.components.ro_table import render_ro_table
    render_ro_table()

elif page == "Inventory":
    from dashboard.components.inventory_panel import render_inventory
    render_inventory()

elif page == "Quotes":
    from dashboard.components.quote_panel import render_quotes
    render_quotes()

elif page == "Purchase Orders":
    from dashboard.components.po_tracker import render_po_tracker
    render_po_tracker()

elif page == "Pending Approval":
    from dashboard.components.pending_approval import render_pending_approval
    render_pending_approval()

elif page == "Evals":
    from dashboard.components.evals_page import render_evals
    render_evals()