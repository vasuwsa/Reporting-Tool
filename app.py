from __future__ import annotations
# Strobes report generator — streamlit run app.py
# Layout: 1. Connection (multi-asset URLs + JWT)  2. Report cards (asset/engagement/template/fields)  3. Review/Generate
import base64
import json
import re
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

from strobes_report import Strobes, parse_asset_url, md_table, md_lines, preview_problems, engagement_scope, report_problems

SAVED_VALUES = Path(__file__).resolve().parent / "saved_values.json"

st.set_page_config(page_title="Strobes report generator", layout="wide", page_icon="strobes-icon.png")

# ---------------------------------------------------------------------------
# theme CSS
# ---------------------------------------------------------------------------

_STROBES_CSS = """
<style>
    /* ---- global overrides for Strobes dark theme ---- */
    .stApp {
        background-color: #0d1117;
    }

    /* primary buttons */
    .stButton > button[kind="primary"],
    .stButton > button[data-testid="stBaseButton-primary"] {
        background-color: #56D364;
        color: #0d1117;
        border: none;
        font-weight: 600;
    }
    .stButton > button[kind="primary"]:hover,
    .stButton > button[data-testid="stBaseButton-primary"]:hover {
        background-color: #3fb950;
        color: #0d1117;
    }

    /* secondary buttons */
    .stButton > button[kind="secondary"],
    .stButton > button[data-testid="stBaseButton-secondary"] {
        border-color: #30363d;
        color: #e6edf3;
    }
    .stButton > button[kind="secondary"]:hover,
    .stButton > button[data-testid="stBaseButton-secondary"]:hover {
        border-color: #56D364;
        color: #56D364;
    }

    /* containers / cards */
    [data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: #30363d;
        background-color: #161b22;
    }

    /* text inputs */
    .stTextInput > div > div > input,
    .stTextArea > div > div > textarea {
        background-color: #0d1117;
        border-color: #30363d;
        color: #e6edf3;
    }
    .stTextInput > div > div > input:focus,
    .stTextArea > div > div > textarea:focus {
        border-color: #56D364;
        box-shadow: 0 0 0 1px #56D364;
    }

    /* selectbox */
    .stSelectbox > div > div {
        background-color: #0d1117;
        border-color: #30363d;
    }

    /* success alert */
    .stAlert [data-testid="stAlertContentSuccess"] {
        background-color: rgba(86, 211, 100, 0.1);
    }

    /* dividers */
    hr {
        border-color: #30363d;
    }

    /* link buttons */
    .stLinkButton > a {
        border-color: #30363d;
        color: #56D364 !important;
    }
    .stLinkButton > a:hover {
        border-color: #56D364;
    }

    /* data editor */
    [data-testid="stDataEditor"] {
        border-color: #30363d;
    }

    /* header bar logo area */
    .strobes-header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 0.5rem;
    }
    .strobes-header img {
        height: 36px;
    }
    .strobes-header .title {
        font-size: 2rem;
        font-weight: 700;
        color: #e6edf3;
    }
    .strobes-header .subtitle {
        font-size: 0.85rem;
        color: #8b949e;
        margin-left: auto;
    }
</style>
"""

st.markdown(_STROBES_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def jwt_expiry(token: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return datetime.fromtimestamp(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def load_saved() -> dict:
    """One shared pool of field values, keyed by custom-field name (shared by every template)."""
    if SAVED_VALUES.exists():
        try:
            data = json.loads(SAVED_VALUES.read_text(encoding="utf-8"))
            return data.get("fields", {}) if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_values(values: dict, template_name: str = ""):
    """Merge this card's values into the pool: known fields are updated, new fields are added.
    Attachments are never stored (images are re-uploaded each time)."""
    pool = load_saved()
    for k, v in values.items():
        if k.endswith("_attachment"):
            continue
        if isinstance(v, str) and not v.strip():
            continue
        pool[k] = v
    SAVED_VALUES.write_text(json.dumps({
        "fields": pool,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "last_template": template_name,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


TABLE_DEFAULTS = {
    "tools_markdown": ["S.No", "Tool Name", "Version", "License Type"],
    "pentesters_markdown": ["S.No", "Consultant Name", "Certifications",
                            "Whether the resource has been listed in the Snapshot information "
                            "Published on CERT-In’s website (Yes/No)"],
}
TABLE_MARKDOWN = {"tools_markdown", "pentesters_markdown"}
TEXT_MARKDOWN = set()
DEFAULTS = {"statement_of_limitations_if_any_markdown":
            "Strobes security team did not have any limitations for this engagement."}
FIXED_COLUMNS = {"tools_markdown"}
OPTIONAL_RE = re.compile(r"^tool(?!1_)\d+_")
ENGAGEMENT_REQUIRED = ["test_location", "assessment_cycle", "client_spoc_name", "client_spoc_role",
                       "client_spoc_email", "reason_for_conducting_testing"]

SCOPE_SOURCE = {
    "Application Name": "asset custom fields", "Staging URL": "asset custom fields",
    "Production URL": "asset custom fields", "Type of Testing": "asset custom fields",
    "Assessment Cycle": "engagement fields", "Location": "engagement fields",
    "Reason for Conducting Testing": "engagement fields", "Public IP Address": "this form",
}


def _scope_hint(empty: list[str]) -> str:
    by = {}
    for k in empty:
        by.setdefault(SCOPE_SOURCE.get(k, "Strobes"), []).append(k)
    return " ".join(f"Fill {', '.join(v)} in {src}." for src, v in by.items()) + " Then sign in again and review."


def is_required(name: str) -> bool:
    return field_kind(name) in ("text", "date", "attachment") and not OPTIONAL_RE.match(name)


def field_kind(name: str) -> str:
    for suffix in ("_markdown", "_date", "_attachment"):
        if name.endswith(suffix):
            return suffix[1:]
    return "text"


def pretty(name: str) -> str:
    base = re.sub(r"_(markdown|date|attachment|markup)$", "", name)
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base)
    base = re.sub(r"(?<=[a-z])(?=\d+$)", " ", base)
    return base.replace("_", " ").capitalize()


def _sig(rep) -> str:
    def norm(v):
        if isinstance(v, list):
            return [(getattr(f, "name", None), getattr(f, "size", None)) for f in v]
        return v
    return json.dumps({"n": rep["name"], "t": rep["template"]["id"] if rep["template"] else None,
                       "v": {k: norm(v) for k, v in rep["values"].items()}}, sort_keys=True, default=str)


def _eng_fields(eng: dict) -> dict:
    try:
        return json.loads(eng.get("fields") or "{}")
    except Exception:
        return {}


def _eng_ok(rep: dict) -> bool:
    """True when this report has a chosen engagement whose required fields are all filled."""
    eng = rep.get("engagement")
    if not eng:
        return False
    required = st.session_state.get("eng_defs") or ENGAGEMENT_REQUIRED
    fields = _eng_fields(eng)
    return not any(not str(fields.get(k) or "").strip() for k in required)


def missing_fields(rep) -> list[str]:
    miss = []
    if not rep.get("asset_id") or rep["asset_id"] not in st.session_state.get("loaded_assets", {}):
        miss.append("Asset")
        return miss
    if not rep.get("engagement"):
        miss.append("Engagement")
        return miss
    if not rep["name"].strip():
        miss.append("Report name")
    if not rep["template"]:
        miss.append("Template")
        return miss
    for f in rep["template"]["customFields"]:
        v = rep["values"].get(f)
        empty = not v if field_kind(f) == "attachment" else not str(v or "").strip()
        if is_required(f) and empty:
            miss.append(pretty(f) + (" (POC image)" if field_kind(f) == "attachment" else ""))
    return miss


def ready(rep):
    return not missing_fields(rep)


def new_report():
    return {
        "uid": uuid.uuid4().hex[:8],
        "ver": 0,
        "name": "",
        "template": None,
        "values": {},
        "status": None,
        "asset_id": None,       # int key into loaded_assets
        "engagement": None,     # selected engagement dict
        "search_query": None,   # built from asset_query + engagement id
        "view_query": None,     # original query from a tag-based view URL
    }


def ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


ss("api", None)
ss("user", None)
ss("loaded_assets", {})    # {asset_id (int): {"asset": dict, "engagements": list, "asset_query": str}}
ss("eng_defs", [])          # org-level engagement field slug list (same for all assets)
ss("url_slots", [uuid.uuid4().hex[:8]])  # stable IDs for each asset URL input field
ss("view_queries", [])     # queries from tag-based view URLs (no asset.id)
ss("reports", [new_report()])
ss("tmpdir", tempfile.mkdtemp(prefix="strobes_poc_"))

# ---------------------------------------------------------------------------
# 1. connection
# ---------------------------------------------------------------------------

_logo_path = Path(__file__).resolve().parent / "strobes-icon.png"
_logo_b64 = base64.b64encode(_logo_path.read_bytes()).decode() if _logo_path.exists() else ""
st.markdown(
    f'<div class="strobes-header">'
    f'<img src="data:image/png;base64,{_logo_b64}" alt="Strobes">'
    f'<span class="title">Strobes report generator</span>'
    f'<span class="subtitle">CTEM Report Automation</span>'
    f'</div>',
    unsafe_allow_html=True,
)

with st.container(border=True):
    st.subheader("Connection")

    # --- step 1: authenticate + sign in ---
    st.markdown("**Authenticate**")
    jwt_col, info_col, btn_col = st.columns([3, 3, 1])
    jwt = jwt_col.text_input(
        "Insert JWT token",
        type="password",
        help="Copy from the browser's local storage (Authorization: JWT ...). Expires every few hours.",
        placeholder="JWT eyJ...",
    )

    # compute url list from previous session state (available before widgets render)
    asset_url_list = [
        st.session_state.get(f"url_{s}", "").strip()
        for s in st.session_state.url_slots
        if st.session_state.get(f"url_{s}", "").strip()
    ]

    exp = jwt_expiry(jwt) if jwt else None
    if exp:
        left = exp - datetime.now()
        if left.total_seconds() > 0:
            info_col.success(f"Token valid · expires {exp.strftime('%H:%M')}")
        else:
            info_col.error("Token has expired — paste a fresh one")
    btn_col.markdown("&nbsp;", unsafe_allow_html=True)
    if btn_col.button("Sign in", type="primary", disabled=not jwt, use_container_width=True):
        if not asset_url_list:
            st.error("Paste at least one Asset / View URL below, then sign in.")
        else:
            try:
                loaded: dict = {}
                view_queries: list = []
                api_inst = None
                user_info = None
                eng_defs: list = []
                url_errors: list = []

                for url in asset_url_list:
                    try:
                        parsed = parse_asset_url(url)
                    except SystemExit as e:
                        url_errors.append(str(e))
                        continue
                    if api_inst is None:
                        api_inst = Strobes(parsed["base_url"], parsed["organization_id"], jwt=jwt)
                        user_info = api_inst.whoami()
                        try:
                            eng_defs = [d["slug"] for d in api_inst.engagement_field_defs()] or ENGAGEMENT_REQUIRED
                        except Exception:
                            eng_defs = ENGAGEMENT_REQUIRED
                    aid = parsed["asset_id"]
                    if aid is None:
                        view_queries.append(parsed["asset_query"])
                        continue
                    try:
                        asset_obj = api_inst.find_asset(aid)
                        engs = api_inst.engagements_for_asset(aid)
                    except Exception as e:
                        url_errors.append(f"Asset {aid}: {e}")
                        continue
                    loaded[aid] = {
                        "asset": asset_obj,
                        "engagements": engs,
                        "asset_query": parsed["asset_query"] or f"asset.id = {aid}",
                    }

                # For tag-based views (no asset.id), load all org assets
                # so the user can pick one in the report card.
                if view_queries and api_inst:
                    try:
                        all_assets = api_inst.list_assets()
                        for a in all_assets:
                            aid = a["id"]
                            if aid not in loaded:
                                engs = api_inst.engagements_for_asset(aid)
                                loaded[aid] = {
                                    "asset": a,
                                    "engagements": engs,
                                    "asset_query": f"asset.id = {aid}",
                                }
                    except Exception as e:
                        url_errors.append(f"Could not load org assets: {e}")

                for err in url_errors:
                    st.error(err)

                if not api_inst:
                    st.error("No valid URLs — check the URLs below and try again.")
                elif loaded:
                    prev = st.session_state.api
                    if prev is None or prev.base != api_inst.base or prev.org != api_inst.org:
                        st.cache_data.clear()

                    prev_ids = set(st.session_state.loaded_assets.keys())
                    new_ids = set(loaded.keys())
                    if new_ids != prev_ids:
                        reports = []
                        # One report per asset-specific URL
                        for url in asset_url_list:
                            try:
                                p = parse_asset_url(url)
                            except SystemExit:
                                continue
                            if p["asset_id"] and p["asset_id"] in loaded:
                                reports.append({**new_report(), "asset_id": p["asset_id"]})
                        # One report per tag-based view (user picks asset in card)
                        for vq in view_queries:
                            reports.append({**new_report(), "view_query": vq})
                        if not reports:
                            reports = [new_report()]
                        st.session_state.reports = reports

                    st.session_state.update(api=api_inst, user=user_info,
                                            loaded_assets=loaded, eng_defs=eng_defs,
                                            view_queries=view_queries)
            except PermissionError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Could not sign in: {e}")

    if st.session_state.user:
        u = st.session_state.user
        la = st.session_state.loaded_assets
        n = len(la)
        st.success(f"Signed in as {u.get('email')}  ·  {n} asset{'s' if n != 1 else ''} loaded")
        for aid, ac in la.items():
            a = ac["asset"]
            ne = len(ac["engagements"])
            st.caption(f"  • Asset {a['id']}: {a['name']}  ({ne} engagement{'s' if ne != 1 else ''})")

    st.divider()

    # --- step 2: assets / views ---
    st.markdown("**Assets / Views**")
    slots = st.session_state.url_slots
    for i, slot_id in enumerate(slots):
        row = st.columns([11, 1])
        row[0].text_input(
            f"Asset / View URL {i + 1}",
            key=f"url_{slot_id}",
            placeholder="https://payg.strobes.co/.../findings?query=asset.id%20in%20(1153)&viewId=...  or  .../assets/1153?query=...",
            help="Paste a Findings saved-view URL (.../findings?query=...) or an Asset page URL (.../assets/<id>). Both are accepted.",
        )
        if len(slots) > 1 and row[1].button("✕", key=f"rm_{slot_id}", help="Remove"):
            st.session_state.url_slots = [s for s in slots if s != slot_id]
            st.rerun()
    if st.button("＋ Add another asset / view", key="add_url"):
        st.session_state.url_slots.append(uuid.uuid4().hex[:8])
        st.rerun()

api: Strobes | None = st.session_state.api
if not api:
    st.info("Paste one or more Findings view URLs (or Asset page URLs) and your JWT, then sign in.")
    st.stop()

loaded_assets: dict = st.session_state.loaded_assets
if not loaded_assets:
    st.info("No assets loaded. Sign in again.")
    st.stop()

# ---------------------------------------------------------------------------
# 2. report cards
# ---------------------------------------------------------------------------

saved = load_saved()
reports = st.session_state.reports


@st.cache_data(show_spinner=False, ttl=300)
def _templates_for(base_url: str, org_id: str, text: str):
    return st.session_state.api.search_templates(text)


def _templates(text: str):
    return _templates_for(api.base, api.org, text)


def render_field(rep: dict, name: str):
    kind = field_kind(name)
    key = f"{rep['uid']}:{rep.get('ver', 0)}:{name}"
    vals = rep["values"]
    label = pretty(name) + (" *" if is_required(name) else "")

    if kind == "markdown" and name not in TABLE_MARKDOWN:
        cur = vals.get(name)
        cur = cur if isinstance(cur, str) else DEFAULTS.get(name, "")
        vals[name] = st.text_area(label, cur, key=key, height=120)

    elif kind == "markdown":
        st.markdown(f"**{label}**")
        base = rep.setdefault("table_base", {})
        tver = rep.setdefault("table_ver", {})
        if name not in base:
            cur = vals.get(name)
            if not isinstance(cur, dict):
                headers = TABLE_DEFAULTS.get(name, ["S.No", "Name", "Details"])
                cur = {"headers": headers, "rows": [["1"] + [""] * (len(headers) - 1)]}
            base[name] = cur
        headers = list(base[name]["headers"])

        def _reset_table(new_headers):
            current = vals.get(name) if isinstance(vals.get(name), dict) else base[name]
            rows = [list(r) + [""] * (len(new_headers) - len(r)) for r in current["rows"]]
            base[name] = {"headers": new_headers, "rows": [r[:len(new_headers)] for r in rows]}
            tver[name] = tver.get(name, 0) + 1

        if name not in FIXED_COLUMNS:
            c1, c2, c3 = st.columns([3, 1, 1])
            newcol = c1.text_input("Add a column", key=key + ":nc", placeholder="Column name", label_visibility="collapsed")
            if c2.button("Add column", key=key + ":add", disabled=not newcol.strip()):
                _reset_table(headers + [newcol.strip()])
                st.rerun()
            if c3.button("Remove last column", key=key + ":rm", disabled=len(headers) <= 1):
                _reset_table(headers[:-1])
                st.rerun()
        src = [dict(zip(headers, r)) for r in base[name]["rows"]]
        edited = st.data_editor(src, num_rows="dynamic", key=f"{key}:t{tver.get(name, 0)}",
                                width="stretch", hide_index=True)
        vals[name] = {"headers": headers, "rows": [[str(r.get(h, "") or "") for h in headers] for r in edited]}

    elif kind == "date":
        cur = vals.get(name)
        try:
            d = datetime.strptime(cur, "%m/%d/%Y").date() if cur else datetime.now().date()
        except Exception:
            d = datetime.now().date()
        d = st.date_input(label, d, key=key)
        vals[name] = d.strftime("%m/%d/%Y")

    elif kind == "attachment":
        files = st.file_uploader(label + " (POC image)", type=["png", "jpg", "jpeg", "gif", "webp"],
                                 accept_multiple_files=True, key=key)
        vals[name] = files or []

    elif name.endswith("_markup"):
        vals[name] = st.text_area(label, vals.get(name, ""), key=key, height=80)

    else:
        vals[name] = st.text_input(label, vals.get(name, ""), key=key)


def report_card(idx: int, rep: dict):
    title = rep["name"] or f"Report {idx + 1}"
    with st.container(border=True):
        top = st.columns([6, 1])
        top[0].markdown(f"#### {title}")
        if len(reports) > 1 and top[1].button("Remove", key=f"rm:{rep['uid']}"):
            reports.pop(idx)
            st.rerun()

        # ---- asset + engagement selectors ----
        asset_ids = list(loaded_assets.keys())
        view_q = rep.get("view_query")

        def _asset_label(aid):
            a = loaded_assets[aid]["asset"]
            return f"Asset {a['id']}: {a['name']}"

        if view_q:
            st.caption(f"View filter: `{view_q}`")

        cur_asset_id = rep.get("asset_id")
        asset_idx = asset_ids.index(cur_asset_id) if cur_asset_id in asset_ids else None

        a1, a2 = st.columns(2)
        picked_asset_id = a1.selectbox(
            "Asset *",
            options=asset_ids,
            format_func=_asset_label,
            index=asset_idx,
            key=f"asset:{rep['uid']}",
            placeholder="Choose an asset",
        )

        if picked_asset_id != cur_asset_id:
            rep["asset_id"] = picked_asset_id
            rep["engagement"] = None
            rep["search_query"] = None
            st.rerun()

        eng_ok_for_rep = False
        if rep.get("asset_id") and rep["asset_id"] in loaded_assets:
            ac = loaded_assets[rep["asset_id"]]
            engs = ac["engagements"]
            if not engs:
                a2.error("No active engagements linked to this asset. Link one in Strobes first.")
            else:
                cur_eng = rep.get("engagement")
                cur_eng_id = cur_eng["id"] if cur_eng else None
                eng_idx = next((i for i, e in enumerate(engs) if e["id"] == cur_eng_id), None)

                picked_eng = a2.selectbox(
                    "Engagement *",
                    engs,
                    format_func=lambda e: e["name"],
                    index=eng_idx,
                    key=f"eng:{rep['uid']}",
                    placeholder="Choose an engagement",
                )

                if picked_eng and (cur_eng is None or picked_eng["id"] != cur_eng_id):
                    rep["engagement"] = picked_eng
                    base_query = view_q if view_q else ac["asset_query"]
                    rep["search_query"] = f'{base_query} and is_active in ("true") and engagements.id = "{picked_eng["id"]}"'
                    st.rerun()

                if rep.get("engagement"):
                    eng = rep["engagement"]
                    fields = _eng_fields(eng)
                    required = st.session_state.get("eng_defs") or ENGAGEMENT_REQUIRED
                    missing_eng = [k for k in required if not str(fields.get(k) or "").strip()]
                    eng_ok_for_rep = not missing_eng
                    if not eng_ok_for_rep:
                        present = ", ".join(f"{k}={fields[k]}" for k in required if k not in missing_eng) or "none"
                        st.error(f"Engagement '{eng['name']}' is missing required fields: {', '.join(missing_eng)}. "
                                 f"Present: {present}. Fill them in Strobes, then sign in again.")
                    else:
                        st.caption("Engagement fields OK  ·  " +
                                   ", ".join(f"{k}={fields[k]}" for k in required if k in fields))
                    if rep.get("search_query"):
                        st.caption(f"Findings filter: `{rep['search_query']}`")

        # ---- report name + template ----
        rep["name"] = st.text_input("Report name *", rep["name"], key=f"name:{rep['uid']}",
                                    placeholder="Client-Vendor-Web-App-FirstAudit Report-DDMMYYYY-V1.0")

        c1, c2 = st.columns([1, 2])
        search = c1.text_input("Search templates *", key=f"tsearch:{rep['uid']}", placeholder="e.g. certin")
        try:
            options = _templates(search) if search else _templates("")
        except PermissionError as e:
            st.error(str(e)); return
        except Exception as e:
            st.error(f"Could not load templates from Strobes: {e}. Click Sign in again to retry.")
            return
        names = [o["templateName"] for o in options]
        cur = rep["template"]["templateName"] if rep["template"] else None
        pick = c2.selectbox("Template *", names, index=names.index(cur) if cur in names else None,
                            key=f"tpl:{rep['uid']}", placeholder="Choose a template")
        if pick and pick != cur:
            rep["template"] = next(o for o in options if o["templateName"] == pick)
            rep["values"] = {}
            rep["table_base"] = {}
            rep["ver"] = rep.get("ver", 0) + 1
            st.rerun()

        tpl = rep["template"]
        if not tpl:
            st.caption("Pick a template to load its fields.")
            return

        b1, b2, _ = st.columns([1, 1, 4])
        known = {k: v for k, v in saved.items() if k in tpl["customFields"]}
        if known and b1.button(f"Load saved values ({len(known)})", key=f"load:{rep['uid']}",
                               help="Fills the fields this template shares with values from earlier reviews"):
            rep["values"].update(known)
            rep["table_base"] = {}
            rep["ver"] = rep.get("ver", 0) + 1
            st.rerun()
        if idx > 0 and b2.button("Copy from report 1", key=f"copy:{rep['uid']}"):
            src = reports[0]["values"]
            rep["values"].update({k: v for k, v in src.items() if k in tpl["customFields"]
                                  and not k.endswith("_attachment")})
            rep["table_base"] = {}
            rep["ver"] = rep.get("ver", 0) + 1
            st.rerun()

        def natural(name):
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]

        fields_sorted = sorted(tpl["customFields"], key=natural)
        text_fields = [f for f in fields_sorted if field_kind(f) in ("text", "date")]
        table_fields = [f for f in fields_sorted if field_kind(f) == "markdown"]
        att_fields = [f for f in fields_sorted if field_kind(f) == "attachment"]

        cols = st.columns(2)
        half = (len(text_fields) + 1) // 2
        for i, f in enumerate(text_fields):
            with cols[0 if i < half else 1]:
                render_field(rep, f)
        for f in table_fields:
            render_field(rep, f)
        if att_fields:
            st.markdown("**Proof-of-concept images**")
            cols = st.columns(min(3, len(att_fields)))
            for i, f in enumerate(att_fields):
                with cols[i % len(cols)]:
                    render_field(rep, f)

        miss = missing_fields(rep)
        if miss:
            st.caption("Required before generating: " + ", ".join(miss))
        if rep["status"]:
            level, msg = rep["status"]
            getattr(st, level)(msg)
        if rep.get("gaps"):
            with st.expander(f"Missing data ({len(rep['gaps'])})", expanded=True):
                by_section = {}
                for g in rep["gaps"]:
                    sec, _, where = g.partition(" > ")
                    by_section.setdefault(sec, []).append(where)
                for sec, items in by_section.items():
                    st.markdown(f"**{sec}**  \n" + "  \n".join(f"- {w}" for w in items))


for i, rep in enumerate(reports):
    report_card(i, rep)

if st.button("Add another report"):
    reports.append(new_report())
    st.rerun()

# ---------------------------------------------------------------------------
# 3. preview / generate
# ---------------------------------------------------------------------------

def build_payload(rep: dict) -> dict:
    tmp = Path(st.session_state.tmpdir)
    custom = {}
    for f in rep["template"]["customFields"]:
        kind = field_kind(f)
        v = rep["values"].get(f)
        if kind == "markdown":
            custom[f] = md_table(v) if isinstance(v, dict) and v.get("rows") else md_lines(v or "")
        elif kind == "attachment":
            ids = []
            for uf in (v or []):
                p = tmp / f"{rep['uid']}_{uf.name}"
                p.write_bytes(uf.getvalue())
                ids.append(api.upload_attachment(p))
            custom[f] = ids
        else:
            custom[f] = v or ""
    return {"custom": custom}


def run(preview_only: bool):
    for rep in reports:
        miss = missing_fields(rep)
        if miss:
            rep["status"] = ("error", "Required: " + ", ".join(miss))
            continue
        if not _eng_ok(rep):
            rep["status"] = ("error", "Engagement is missing required fields. Fill them in Strobes and sign in again.")
            continue
        try:
            with st.spinner(f"{'Reviewing' if preview_only else 'Generating'} {rep['name']}..."):
                custom = build_payload(rep)
                kw = dict(
                    search_query=rep["search_query"],
                    engagement_id=rep["engagement"]["id"],
                    template_id=rep["template"]["id"],
                    report_name=rep["name"],
                    custom_data=custom,
                )
                html = api.preview_report(**kw)
                if preview_only:
                    save_values(rep["values"], rep["template"]["templateName"])
                rep["preview_html"] = html
                page_error = preview_problems(html)
                if page_error:
                    rep["gaps"] = [f"TEMPLATE ERROR > {p}" for p in page_error]
                    rep["status"] = ("error", "The template did not render a report: " + "; ".join(page_error))
                    rep["review_ok"] = False
                    continue
                gaps = report_problems(html)
                rep["gaps"] = gaps
                if gaps:
                    scope_empty = [k for k, v in engagement_scope(html).items() if not v]
                    hint = (" " + _scope_hint(scope_empty)) if scope_empty else ""
                    rep["status"] = ("error", f"Report is not fully enriched - {len(gaps)} missing value(s) "
                                     "across the report. See 'Missing data' below." + hint)
                    rep["review_ok"] = False
                    continue
                rep["review_ok"] = True
                rep["gaps"] = []
                rep["reviewed_sig"] = _sig(rep)
                if preview_only:
                    rep["status"] = ("success", "Review OK - every table and field in the rendered report has data. "
                                     "Use 'Open preview' below to read it.")
                    continue
                msg = api.generate_report(**kw)
                save_values(rep["values"], rep["template"]["templateName"])
                rep["status"] = ("success", msg)
        except Exception as e:
            rep["status"] = ("error", f"Failed: {e}")


st.divider()
all_eng_ok = all(_eng_ok(r) for r in reports)
b1, b2, _ = st.columns([1, 1, 4])
if not all_eng_ok:
    st.caption("Engagement required fields must be filled in Strobes for every report before reviewing.")
if b1.button("Review all", disabled=not all_eng_ok):
    run(preview_only=True)
    st.rerun()
all_reviewed = all(r.get("review_ok") and r.get("reviewed_sig") == _sig(r) for r in reports)
if not all_reviewed:
    st.caption("Generate is enabled once every report has passed Review all with its current values.")
if b2.button("Generate all", type="primary",
             disabled=not (all_eng_ok and all(ready(r) for r in reports) and all_reviewed)):
    run(preview_only=False)
    st.rerun()


@st.cache_resource(show_spinner=False)
def preview_server():
    """Serve preview HTML files from a temp dir on localhost so they can open in a new tab."""
    import http.server, socketserver, threading
    root = Path(tempfile.mkdtemp(prefix="strobes_previews_"))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a, **k):
            pass

        def handle(self):
            try:
                super().handle()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    srv = Server(("127.0.0.1", 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return root, srv.server_address[1]


PREVIEW_ROOT, PREVIEW_PORT = preview_server()

for rep in reports:
    if not rep.get("preview_html"):
        continue
    f = PREVIEW_ROOT / f"{rep['uid']}_{rep.get('ver', 0)}.html"
    f.write_text(rep["preview_html"], encoding="utf-8")
    c1, c2, _ = st.columns([1, 1, 4])
    c1.link_button(f"Open preview: {rep['name']}", f"http://127.0.0.1:{PREVIEW_PORT}/{f.name}?t={int(time.time())}")
    c2.download_button("Download HTML", rep["preview_html"], file_name=f"{rep['name']}.preview.html",
                       mime="text/html", key=f"dl:{rep['uid']}")

if any(r["status"] and r["status"][0] == "success" and "generated" in r["status"][1] for r in reports):
    st.info("Generation runs in the background on Strobes. Open the Reports page there to download the PDFs.")
