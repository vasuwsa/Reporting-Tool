# Strobes Report Generator

A Streamlit app that automates CTEM report generation using the Strobes platform API.

## Features

- Multi-asset report generation from a single session
- Supports Asset page URLs and Findings saved-view URLs (including tag-based and scan-based views)
- Template selection with custom field editing (text, tables, dates, POC image uploads)
- Engagement validation with required-field checking
- Report preview with gap detection before generation
- Persistent field values across sessions (shared across templates)
- Copy values between reports in the same session

## Supported Report Types

- **Web Application VAPT** — paste an asset URL or view url
- **Mobile Application VAPT** — paste an asset URL or view url
- **Network VAPT** — paste a findings view URL (`/findings?query=...&viewId=...`) with scan-based or tag-based filters

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
streamlit run app.py --server.headless true
```

1. Paste one or more Asset or Findings view URLs
2. Paste your Strobes JWT token and click **Sign in**
3. Select a template and engagement for each report
4. Fill in the custom fields (use **Load saved values** to reuse previous entries)
5. Click **Review all** to validate — fix any missing data flagged
6. Click **Generate all** to create the reports on Strobes

## URL Formats

| Type | URL Pattern | Example |
|------|-------------|---------|
| Asset | `.../assets/<id>` | `.../assets/1153` |
| Asset + query | `.../assets/<id>?query=...` | `.../assets/1153?query=state in (1)` |
| Findings view | `.../findings?query=...&viewId=...` | `.../findings?query=(scan.task_id="...")&viewId=175` |

## Requirements

- Python 3.9+
