# Strobes Report Generator

A Streamlit app that automates multiple reports generation.

## Features

- Multi-asset report generation from a single session
- Supports Asset page URLs and Findings saved-view URLs (including tag-based views)
- Template selection with custom field editing
- Engagement scope auto-detection
- Report preview and validation before generation
- Persistent field values across sessions

## Supported Report Types

- Web Application VAPT
- Mobile Application VAPT
- Network VAPT (coming soon)

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
streamlit run app.py
```

1. Paste your Strobes JWT token and click **Sign in**
2. Add one or more Asset or Findings view URLs
3. Select a template and engagement for each report
4. Fill in the custom fields
5. Click **Generate** to create the report

## Requirements

- Python 3.9+
