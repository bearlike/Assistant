# App component library

Copy-paste page components for Mewbo App frontends. Each file packages **one**
component as a single class: state in instance variables, rendering in one
`render()` method. Same shape every time, so you only learn it once.

## How to use one

Extract the class block (everything between the `# ── <ClassName> ─` and
`# ── end <ClassName> ─` markers) and paste it into your frontend file — no
cross-file imports. Then feed it live data from the SDK and call `.render()`:

```python
import streamlit as st
import mewbo_app

app = mewbo_app.connect()

# ... pasted DataTablePage class ...

DataTablePage(app.data.query("emails", limit=200)).render()
```

## The components

| Data shape | File | Class |
|---|---|---|
| Any list of documents | `data_table_page.py` | `DataTablePage` |
| A row of summary numbers | `metric_header.py` | `MetricHeader` |
| Pipeline run history | `freshness_strip.py` | `FreshnessStrip` |

## Rules (same as the app builder)

- **An app is a page** — these components use `st.header`/`st.subheader` freely.
- **Data comes from the SDK** — pass `app.data.query(...)` / `app.system.*` results
  into a component; never fetch inside it.
- **No raw network, no `st.set_page_config()`** — the submit-time lint gate enforces both.
