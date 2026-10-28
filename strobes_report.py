#!/usr/bin/env python3
from __future__ import annotations
"""
Automate Strobes report generation (template + engagement + custom fields + POC uploads).

Flow (reverse-engineered from a Burp capture of the UI):
  --. asset_url in config is parsed for base_url / organization_id / asset_id / ?query=
  0. allAssets(id=N)       -> validate asset; allEngagements(engagement_assessment.asset.id = N)
                              gives the engagements the asset page offers in its dropdown
  1. allTemplates        -> template id + list of customFields the template expects
  2. allEngagements      -> engagement uuid
  3. addReportAttachment -> upload each POC image (multipart GraphQL), get attachment ids
  4. previewReport       -> render once; abort if the template's pre-check fails
  5. generateReport      -> kick off async PDF generation
  6. ws /export/         -> (optional) listen for progress messages

Auth: the API is authenticated by "Authorization: JWT <token>" (browser keeps it in local
storage; there is no session cookie). Cookies (csrftoken, AWS ALB stickiness) are sent too if
provided. Supply via env STROBES_JWT / STROBES_COOKIE or config["auth"].

Usage:
  python strobes_report.py config.json [--no-preview] [--no-ws] [--dry-run] [--save-preview out.html]
"""
import argparse
import json
import re
import os
import sys
import time
from pathlib import Path

import requests
from urllib.parse import urlparse, parse_qs, unquote

ASSET_URL_RE    = re.compile(r"/organizations/(?P<org>[0-9a-f-]{36})/apps/[^/]+/assets/(?P<asset>\d+)")
FINDINGS_URL_RE = re.compile(r"/organizations/(?P<org>[0-9a-f-]{36})/apps/[^/]+/findings")
ASSET_IN_QUERY_RE = re.compile(r"asset\.id\s*(?:=|in\s*\()\s*(\d+)")


def parse_asset_url(url: str) -> dict:
    """
    Accepts two URL shapes:
      Asset page:    .../assets/<id>?query=...
      Findings page: .../findings?query=asset.id%20in%20(1153)%20and%20state%20in%20(1)
    Returns {base_url, organization_id, asset_id, asset_query}.
    """
    u = urlparse(url)
    q = " ".join(unquote(parse_qs(u.query).get("query", [""])[0]).split())

    m = ASSET_URL_RE.search(u.path)
    if m:
        return {
            "base_url": f"{u.scheme}://{u.netloc}",
            "organization_id": m.group("org"),
            "asset_id": int(m.group("asset")),
            "asset_query": q,
        }

    mf = FINDINGS_URL_RE.search(u.path)
    if mf:
        ma = ASSET_IN_QUERY_RE.search(q)
        return {
            "base_url": f"{u.scheme}://{u.netloc}",
            "organization_id": mf.group("org"),
            "asset_id": int(ma.group(1)) if ma else None,
            "asset_query": q,
        }

    raise SystemExit(
        f"Unrecognised URL: {url}\n"
        "Paste either an Asset page URL (.../assets/<id>) or a Findings page URL (.../findings?query=...)."
    )

# ---------------------------------------------------------------------------
# GraphQL client
# ---------------------------------------------------------------------------

class Strobes:
    def __init__(self, base_url: str, org_id: str, jwt: str | None = None, cookie: str | None = None):
        self.base = base_url.rstrip("/")
        self.org = org_id
        self.jwt = jwt
        self.s = requests.Session()
        if cookie:
            # raw "Cookie:" header value copied from the browser/Burp
            for part in cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    self.s.cookies.set(k, v)
            csrf = self.s.cookies.get("csrftoken")
            if csrf:
                self.s.headers["X-CSRFToken"] = csrf
        if jwt:
            self.s.headers["Authorization"] = f"JWT {jwt}"
        self.s.headers.update({
            "App-Name": "ctem",
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base,
            "Referer": f"{self.base}/organizations/{org_id}/apps/ctem/reports",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) strobes-report-automation/1.0",
        })

    def _post(self, **kw) -> requests.Response:
        """POST with one retry on a dropped keep-alive connection (load balancer idle timeout)."""
        for attempt in (1, 2):
            try:
                return self.s.post(f"{self.base}/api/graphql/", timeout=60, **kw)
            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError):
                if attempt == 2:
                    raise
                self.s.close()          # drop the stale pooled socket; next call opens a fresh one
                time.sleep(0.5)

    def gql(self, query: str, variables: dict | None = None, operation_name: str | None = None) -> dict:
        payload = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        if operation_name:
            payload["operationName"] = operation_name
        r = self._post(json=payload)
        if r.status_code in (401, 403):
            raise PermissionError("Strobes rejected the token (401/403) - it has probably expired. Paste a fresh JWT and sign in again.")
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"GraphQL error: {json.dumps(data['errors'], indent=2)}")
        return data["data"]

    # -- lookups -------------------------------------------------------------

    def whoami(self) -> dict:
        """REST call the UI makes on load; cheapest way to validate the token."""
        r = self.s.get(f"{self.base}/api/v1/user/", timeout=30)
        if r.status_code in (401, 403):
            raise PermissionError("Token rejected (401/403) - expired or wrong host?")
        r.raise_for_status()
        return r.json()

    def search_templates(self, text: str, page_size: int = 20) -> list[dict]:
        """Typeahead used by the Report Template dropdown."""
        sq = f'template_name ~ \\"{_esc(text)}\\"' if text else "type != 1"
        q = f'''{{
          allTemplates(organizationId:"{self.org}", searchQuery:"{sq}", page:1, pageSize:{page_size}) {{
            objects {{ id templateName customFields isActive }}
          }}
        }}'''
        return self.gql(q)["allTemplates"]["objects"]

    def find_template(self, name: str) -> dict:
        q = f'''{{
          allTemplates(organizationId:"{self.org}",
                       searchQuery:"template_name ~ \\"{_esc(name)}\\"",
                       page: 1, pageSize: 20) {{
            objects {{ id templateName customFields isActive }}
          }}
        }}'''
        objs = self.gql(q)["allTemplates"]["objects"]
        exact = [o for o in objs if o["templateName"] == name]
        if not exact:
            names = [o["templateName"] for o in objs]
            raise SystemExit(f"Template '{name}' not found. Close matches: {names}")
        return exact[0]

    def find_engagement(self, name: str) -> dict:
        q = f'''{{
          allEngagements(organizationId:"{self.org}",
                         searchQuery:"name ~ \\"{_esc(name)}\\" and is_active in (\\"true\\")",
                         orderBy:["-created"], page:1, pageSize:20) {{
            objects {{ id name fields isActive state }}
          }}
        }}'''
        objs = self.gql(q)["allEngagements"]["objects"]
        exact = [o for o in objs if o["name"] == name]
        if not exact:
            names = [o["name"] for o in objs]
            raise SystemExit(f"Engagement '{name}' not found. Close matches: {names}")
        return exact[0]

    def list_assets(self, page_size: int = 50) -> list[dict]:
        q = f'''{{
          allAssets(organizationId:"{self.org}", searchQuery:"is_active in (\\"true\\")",
                   page:1, pageSize:{page_size}) {{
            objects {{ id name target type isActive }}
          }}
        }}'''
        return self.gql(q)["allAssets"]["objects"]

    def find_asset(self, asset_id: int | str) -> dict:
        q = f'''{{
          allAssets(organizationId:"{self.org}", searchQuery:"id={int(asset_id)}") {{
            objects {{ id name target type isActive }}
          }}
        }}'''
        objs = self.gql(q)["allAssets"]["objects"]
        if not objs:
            raise SystemExit(f"Asset {asset_id} not found in org {self.org}")
        return objs[0]

    def engagements_for_asset(self, asset_id: int | str) -> list[dict]:
        """Same query the asset page runs to populate the Engagement dropdown."""
        q = f'''{{
          allEngagements(organizationId:"{self.org}",
                         searchQuery:"engagement_assessment.asset.id = {int(asset_id)} and is_active in (\\"true\\")",
                         orderBy:["-created"], page:1, pageSize:50) {{
            objects {{ id name fields isActive state }}
          }}
        }}'''
        return self.gql(q)["allEngagements"]["objects"]

    def list_engagements(self, page_size: int = 50) -> list[dict]:
        q = f'''{{
          allEngagements(organizationId:"{self.org}",
                         searchQuery:"is_active in (\\"true\\")",
                         orderBy:["-created"], page:1, pageSize:{page_size}) {{
            objects {{ id name fields isActive state }}
          }}
        }}'''
        return self.gql(q)["allEngagements"]["objects"]

    def engagement_field_defs(self) -> list[dict]:
        """Org-level engagement custom field definitions (what the template pre-check looks at)."""
        q = f'''{{
          allEngagementsFields(organizationId:"{self.org}", page:1, pageSize:100) {{
            objects {{ id name slug isActive isRequired }}
          }}
        }}'''
        return [f for f in self.gql(q)["allEngagementsFields"]["objects"] if f.get("isActive", True)]

    # -- uploads -------------------------------------------------------------

    _upload_cache: dict = {}

    def upload_attachment(self, path: Path) -> str:
        """GraphQL multipart upload spec: operations / map / file. Cached per file path."""
        key = str(path.resolve())
        if key in self._upload_cache:
            return self._upload_cache[key]
        operations = {
            "query": ("mutation AddReportAttachmentMutation($image: Upload!, $organization: UUID!) "
                      "{addReportAttachment(organizationId: $organization, file: $image) "
                      "{attachment {id attachment attachmentName attachedBy { email } } } }"),
            "variables": {"image": None, "organization": self.org},
            "operationName": "AddReportAttachmentMutation",
        }
        files = {
            "operations": (None, json.dumps(operations), "application/json"),
            "map": (None, json.dumps({"file": ["variables.image"]}), "application/json"),
            "file": (path.name, path.open("rb"), _mime(path)),
        }
        # requests sets multipart Content-Type with boundary automatically
        r = self._post(files=files)
        if r.status_code in (401, 403):
            raise PermissionError("Strobes rejected the token (401/403) - it has probably expired. Paste a fresh JWT and sign in again.")
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"Upload failed for {path}: {data['errors']}")
        att = data["data"]["addReportAttachment"]["attachment"]
        print(f"  uploaded {path.name} -> attachment id {att['id']}")
        self._upload_cache[key] = str(att["id"])
        return self._upload_cache[key]

    # -- report --------------------------------------------------------------

    def _report_args(self, search_query, engagement_id, template_id, report_name, custom_data) -> str:
        return f'''
            organizationId: "{self.org}"
            searchQuery: "{_esc(search_query)}"
            engagementId: "{engagement_id}"
            templateId: {int(template_id)},
            reportName: "{_esc(report_name)}",
            customData: "{_esc(json.dumps(custom_data))}"
        '''

    def preview_report(self, **kw) -> str:
        q = f"{{ previewReport({self._report_args(**kw)}) }}"
        return self.gql(q)["previewReport"]

    def generate_report(self, **kw) -> str:
        q = f"mutation GenerateReportMutation {{ generateReport({self._report_args(**kw)}) {{ reports }} }}"
        return self.gql(q)["generateReport"]["reports"]

    # -- websocket progress ----------------------------------------------------

    def listen_export_ws(self, timeout_s: int = 300):
        try:
            import websocket  # pip install websocket-client
        except ImportError:
            print("  (pip install websocket-client to watch generation progress)")
            return
        url = self.base.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{url}/ws/{self.org}/export/?token={self.jwt}"
        ws = websocket.create_connection(url, origin=self.base, timeout=timeout_s)
        print("  listening on export websocket ...")
        start = time.time()
        try:
            while time.time() - start < timeout_s:
                msg = ws.recv()
                print("  ws:", msg[:300])
                low = msg.lower()
                # don't break on the first completion: several reports may be in flight
                if "error" in low or "failed" in low:
                    print("  (a report reported failure)")
        except Exception as e:  # timeout / closed
            print(f"  ws closed: {e}")
        finally:
            ws.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    """Escape a python string for embedding inside a GraphQL double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _mime(p: Path) -> str:
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}.get(p.suffix.lower(), "application/octet-stream")


ERROR_MARKERS = ("report generation failed", "report has errors", "missing fields",
                 "fix the following", "missing or empty", "please configure these fields")


def preview_problems(html_text: str) -> list[str] | None:
    """
    Inspect a previewReport result. Returns None when the report rendered cleanly,
    otherwise a list of problem strings (missing field names when the template lists them,
    else the error text itself). Templates emit different error pages, e.g.
      "Report Generation Failed / Missing Required Engagement Fields / <li><strong>x</strong>"
      "Report has errors. Please fix the following missing fields before continuing: * tool1_attachment"
    """
    import html as _html
    body = re.sub(r"<(style|script)\b.*?</\1>", " ", html_text, flags=re.S | re.I)
    text = _html.unescape(re.sub(r"<[^>]+>", "\n", body))
    lines = [l.strip(" \t*-\u2022") for l in text.splitlines()]
    lines = [l for l in lines if l]
    low = "\n".join(lines).lower()
    hit = next((m for m in ERROR_MARKERS if m in low), None)
    # a full report can contain per-finding validation text ("... are missing"); only treat the
    # page as an error page when the report body itself is absent
    has_body = bool(re.search(r'id="(engagement-scope|introduction|executive-summary)"|table-of-contents', html_text, re.I))
    if has_body:
        return None
    if hit is None:
        # a real report is tens of KB; a tiny page with no marker is still suspicious
        if len(lines) < 15 and len(html_text) < 4000:
            return ["Preview returned almost no content: " + " ".join(lines)[:300]]
        return None
    # field names inside <li> / <strong>, else bullet-ish lines after the marker
    items = re.findall(r"<li[^>]*>(?:\s*<strong>)?\s*([^<]+?)\s*(?:</strong>)?\s*</li>", body, flags=re.I)
    items = [i.strip() for i in items if i.strip()]
    if not items:
        start = next((n for n, l in enumerate(lines) if hit in l.lower()), 0)
        items = [l for l in lines[start + 1:start + 15]
                 if re.fullmatch(r"[A-Za-z0-9_.\- ]{2,80}", l) and " " not in l.strip()] or lines[start:start + 6]
    return items


def engagement_scope(html_text: str) -> dict[str, str]:
    """
    Read the '2. ENGAGEMENT SCOPE' table out of a rendered preview -> {label: value}.
    Empty dict if the section isn't there (different template, or an error page).
    """
    import html as _html
    m = re.search(r'id="engagement-scope".*?<table[^>]*>(.*?)</table>', html_text, flags=re.S | re.I)
    if not m:
        return {}
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), flags=re.S | re.I):
        cells = [_html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)]
        if len(cells) >= 2:
            out[cells[0]] = " ".join(cells[1].split())
    return out


def _in_findings(section: str) -> bool:
    return section.startswith("8") or any(w in section.upper() for w in ("VULNERAB", "OBSERVATION", "FINDING"))


def report_problems(html_text: str) -> list[str]:
    """
    Walk every section of a rendered report and list the places where data did not get enriched:
      * any table cell that is empty (label/value tables -> "Label", header tables -> "row N / Column")
      * label/value pairs written as <b>Label:</b> or <strong>Label</strong> with nothing after them
        (how the vulnerability blocks - Title, Attributing Factor, CVSS ... - are laid out)
    Each problem is "SECTION > where". Empty when the report is fully enriched.
    """
    import html as _html
    body = re.sub(r"<(style|script)\b.*?</\1>", " ", html_text, flags=re.S | re.I)
    body = re.sub(r'<div class="toc.*?</div>\s*</div>', " ", body, count=1, flags=re.S | re.I)  # skip TOC
    txt = lambda x: " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", x)).split())
    problems, section, finding = [], "COVER PAGE", None
    token = re.compile(r'<div[^>]*class="title[^"]*"[^>]*>(.*?)</div>|<table[^>]*>(.*?)</table>|'
                       r'<(?:b|strong|label)[^>]*>\s*([^<:]{2,60}?)\s*:?\s*</(?:b|strong|label)>\s*:?\s*([^<]*)(?=<)|'
                       r'<h[1-6][^>]*>(.*?)</h[1-6]>|'
                       r'((?:missing|errors?|fix the following)[^<]{0,120})</[^>]+>\s*(?:<[^>]+>\s*)*?<ul[^>]*>(.*?)</ul>',
                       flags=re.S | re.I)

    def where():
        return f"{section} > {finding}" if finding else section

    for m in token.finditer(body):
        if m.group(1) is not None:                       # section title
            t = txt(m.group(1))
            if t:
                section, finding = t, None
            continue
        if m.group(5) is not None:                       # <h*> heading = finding title inside findings
            t = txt(m.group(5))
            if t and _in_findings(section):
                finding = t
            continue
        if m.group(6) is not None:                       # template's own validation list
            items = [txt(i) for i in re.findall(r"<li[^>]*>(.*?)</li>", m.group(7), flags=re.S | re.I)]
            for it in items:
                if it:
                    problems.append(f"{where()} > {it}")
            continue
        if m.group(2) is not None:
            rows = [[txt(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S | re.I)]
                    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(2), flags=re.S | re.I)]
            rows = [r for r in rows if r and any(r)]           # drop filler / spacer rows
            if not rows:
                continue
            header = rows[0] if len(rows) > 1 and len(rows[0]) > 2 else None
            for n, r in enumerate(rows):
                if header is not None and n == 0:
                    continue
                for j, c in enumerate(r):
                    if c:
                        continue
                    if header is not None:
                        col = header[j] if j < len(header) else f"col {j + 1}"
                        problems.append(f"{where()} > row {n} ({r[0] or '?'}) / {col}")
                    elif len(r) == 2 and j == 1:
                        problems.append(f"{where()} > {r[0]}")
                    elif j > 0:
                        problems.append(f"{where()} > {r[0] or 'row ' + str(n + 1)} / col {j + 1}")
            continue
        # bold-label pairs only matter inside the findings sections
        label, value = m.group(3).strip(), (m.group(4) or "").strip()
        if _in_findings(section) and label and not value and not label.isdigit():
            problems.append(f"{where()} > {label}")
    # de-dupe, keep order
    seen, out = set(), []
    for pr in problems:
        if pr not in seen:
            seen.add(pr); out.append(pr)
    return out


def scope_problems(html_text: str) -> list[str]:
    """Labels in the ENGAGEMENT SCOPE table that rendered empty."""
    scope = engagement_scope(html_text)
    return [k for k, v in scope.items() if not v]


def md_lines(text: str) -> str:
    """
    Free text typed into a *_markdown field. The template renders it with markdownify, where a
    single newline is just a space - so a list of URLs collapses into one paragraph. Turn every
    line break into a markdown hard break (two trailing spaces) so lines stay on their own line.
    Blank lines (paragraphs) and existing markdown (bullets, headings) are left alone.
    """
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        line = line.rstrip()
        out.append(line + "  " if line else "")
    return "\n".join(out)


def md_table(spec: dict) -> str:
    """{'headers': [...], 'rows': [[...], ...]} -> markdown table (what the API expects)."""
    hdr = "| " + " | ".join(spec["headers"]) + " |"
    sep = "| " + " | ".join("---" for _ in spec["headers"]) + " |"
    rows = ["| " + " | ".join(str(c) for c in r) + " |" for r in spec["rows"]]
    return "\n".join([hdr, sep, *rows])


def build_custom_data(fields: dict, template_fields: list[str], api: Strobes,
                      base_dir: Path, dry_run: bool) -> dict:
    """Coerce config values into the shapes the template expects, uploading attachments."""
    out = {}
    for key in template_fields:
        if key not in fields:
            print(f"  WARNING: template field '{key}' not in config -> sending empty")
            out[key] = [] if key.endswith("_attachment") else ""
            continue
        val = fields[key]
        if key.endswith("_markdown") and isinstance(val, dict):
            out[key] = md_table(val)
        elif key.endswith("_markdown"):
            out[key] = md_lines(str(val))
        elif key.endswith("_attachment"):
            paths = [val] if isinstance(val, str) else list(val)
            ids = []
            for p in paths:
                fp = (base_dir / p).resolve()
                if not fp.exists():
                    raise SystemExit(f"Attachment not found: {fp}")
                ids.append("DRY" if dry_run else api.upload_attachment(fp))
            out[key] = ids
        elif key.endswith("_date"):
            out[key] = str(val)  # MM/DD/YYYY
        else:
            out[key] = str(val)
    extra = set(fields) - set(template_fields)
    if extra:
        print(f"  NOTE: config fields not used by template: {sorted(extra)}")
    return {"custom": out}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", type=Path)
    ap.add_argument("--no-preview", action="store_true", help="skip previewReport sanity check")
    ap.add_argument("--no-ws", action="store_true", help="don't listen on export websocket")
    ap.add_argument("--dry-run", action="store_true", help="resolve ids, build payload, don't upload/generate")
    ap.add_argument("--save-preview", type=Path, help="write preview HTML here (suffixed _1, _2, ... per report)")
    ap.add_argument("--stop-on-error", action="store_true", help="abort remaining reports after the first failure")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    if cfg.get("asset_url"):
        parsed = parse_asset_url(cfg["asset_url"])
        # URL wins over any hand-typed base_url / organization_id / asset_id
        cfg.update({k: v for k, v in parsed.items() if k != "asset_query"})
        cfg.setdefault("asset_query", parsed["asset_query"])
        print(f"[url] org={cfg['organization_id']} asset={cfg['asset_id']} query={cfg['asset_query']!r}")
    auth = cfg.get("auth", {})
    jwt = os.environ.get("STROBES_JWT") or auth.get("jwt")
    cookie = os.environ.get("STROBES_COOKIE") or auth.get("cookie")
    if not jwt and not cookie:
        raise SystemExit("Provide auth: STROBES_JWT / STROBES_COOKIE env vars or config['auth'].")
    if not jwt:
        print("WARNING: no JWT provided. In the captured traffic the API is authenticated by the "
              "'Authorization: JWT' header, not by cookies (there is no session cookie) - expect 401s.")

    api = Strobes(cfg["base_url"], cfg["organization_id"], jwt=jwt, cookie=cookie)

    asset = None
    if cfg.get("asset_id"):
        print("[0] resolving asset")
        asset = api.find_asset(cfg["asset_id"])
        print(f"  id={asset['id']} name={asset['name']}")

    print("[1] resolving engagement")
    if cfg.get("engagement_name"):
        eng = api.find_engagement(cfg["engagement_name"])
        if asset:
            linked = {e["id"] for e in api.engagements_for_asset(asset["id"])}
            if eng["id"] not in linked:
                print(f"  WARNING: engagement '{eng['name']}' is not linked to asset {asset['id']}")
    elif asset:
        linked = api.engagements_for_asset(asset["id"])
        if len(linked) != 1:
            raise SystemExit(f"Asset {asset['id']} has {len(linked)} linked engagements; "
                             f"set engagement_name in config. Options: {[e['name'] for e in linked]}")
        eng = linked[0]
        print(f"  auto-selected from asset: {eng['name']}")
    else:
        raise SystemExit("Need engagement_name or asset_url/asset_id in config.")
    print(f"  id={eng['id']} fields={eng.get('fields')}")
    if eng.get("fields") in (None, "{}", ""):
        print("  WARNING: engagement custom fields are empty; template pre-checks will likely fail.")

    if cfg.get("asset_query"):
        base_q = cfg["asset_query"]
    elif cfg.get("asset_id"):
        base_q = f'asset.id = {cfg["asset_id"]}'
    else:
        base_q = None
    # the URL's ?query= is used verbatim (no is_active clause added) so the report gets exactly
    # the findings shown on the asset page; engagements.id scopes it to the chosen engagement.
    if base_q:
        search_query = f'{base_q} and is_active in ("true") and engagements.id = "{eng["id"]}"'
    else:
        search_query = f'is_active in ("true") and engagements.id = "{eng["id"]}"'
    print(f"  searchQuery: {search_query}")

    # ---- one entry per report ---------------------------------------------
    # config["reports"] = [{"template_name": ..., "report_name": ..., "custom_fields": {...overrides}}]
    # Legacy single-report config (template_name/report_name at top level) still works.
    reports = cfg.get("reports") or [{"template_name": cfg["template_name"], "report_name": cfg["report_name"]}]
    shared_fields = cfg.get("custom_fields", {})
    results = []

    for n, rep in enumerate(reports, 1):
        tag = f"[{n}/{len(reports)}]"
        print(f"\n{tag} template: {rep['template_name']}")
        tpl = api.find_template(rep["template_name"])
        print(f"  id={tpl['id']} fields={tpl['customFields']}")

        fields = {**shared_fields, **rep.get("custom_fields", {})}
        custom = build_custom_data(fields, tpl["customFields"], api, args.config.parent, args.dry_run)

        report_name = rep.get("report_name") or cfg.get("report_name") or f"{tpl['templateName']} - {eng['name']}"
        kw = dict(search_query=search_query, engagement_id=eng["id"], template_id=tpl["id"],
                  report_name=report_name, custom_data=custom)

        if args.dry_run:
            print(f"{tag} [dry-run] reportName={report_name!r}")
            print(json.dumps(custom, indent=2))
            results.append((report_name, "dry-run"))
            continue

        if not args.no_preview:
            html = api.preview_report(**kw)
            if args.save_preview:
                out = args.save_preview.with_name(f"{args.save_preview.stem}_{n}{args.save_preview.suffix or '.html'}")
                out.write_text(html)
                print(f"  preview saved -> {out}")
            problems = preview_problems(html) or report_problems(html)
            if problems:
                print(f"{tag} SKIPPED - template reported errors: {problems}")
                results.append((report_name, "skipped: " + ", ".join(problems)[:40]))
                if args.stop_on_error:
                    break
                continue
            print(f"  preview OK ({len(html)} chars)")

        try:
            msg = api.generate_report(**kw)
            print(f"  generate -> {msg}")
            results.append((report_name, msg))
        except Exception as e:
            print(f"{tag} FAILED: {e}")
            results.append((report_name, f"failed: {e}"))
            if args.stop_on_error:
                break

    print("\n==== summary ====")
    for name, status in results:
        print(f"  {status:45.45}  {name}")

    if not args.no_ws and jwt and not args.dry_run:
        print("\n[progress] (one websocket, shared by all exports)")
        api.listen_export_ws(timeout_s=300 * len(reports))


if __name__ == "__main__":
    main()
