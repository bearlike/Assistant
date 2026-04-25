# Widget Examples

Read this file first, then explore the subdirectories for working stlite code.

## What a widget is for

A widget presents **one focused result from an agentic task** — a ranked list, a metric snapshot, a chart, or a diagram. It is not a dashboard or an app. The data is already gathered; the widget just displays it clearly.

## Rules (always follow)

- **One result, one view.** Output a single `st.container(border=True)` (or one loop of identical cards). Never build a multi-section page with `st.header`/`st.subheader`/`st.divider`. Don't mix chart types — pick the one that best shows the result.
- **Component catalog first.** Before writing layout code from scratch, check if the data shape matches a catalog card (GitHubRepoCard, SearchResultCard, StockTickerCard, PlantUMLCard). Use the catalog when it fits — it's more consistent and requires less code.
- **Streamlit-native only.** Compose from `st.container(border=True)`, `st.metric(chart_data=…, chart_type="area")`, `st.columns`, `st.caption`, `st.image`, `st.link_button`, inline badges (`:blue-badge[text]`), inline colored text (`:red[-2.13%]`), and material icons (`:material/star:`). Reach for `unsafe_allow_html=True` only after proving no primitive fits.
- `app.py` reads state from `data.json` via `open("data.json")` — no live network calls.
- Use only pure-Python packages available in stlite (pandas, plotly, altair — but most cards don't need any of them).
- Keep `data.json` self-contained; all state the widget needs must be in this file.
- The widget is linted with a stlite-aware ruff config (`ruff.toml` in this folder) plus the AST lint rules in `../linter.py` — import only allowed modules, and do not use forbidden patterns.

## What NOT to do (lint gate enforces some)

| Pattern | Why |
|---|---|
| `st.sidebar.*` | **Lint fail.** The sidebar DOM is absent in the widget panel — it doesn't render. |
| `st.set_page_config()` | **Lint fail.** The console owns stlite config; this call conflicts. |
| `st.header()` / `st.subheader()` / `st.divider()` | Makes a page, not a widget. One container, no section breaks. |
| Multiple distinct chart types | Pick the one view that best shows the result — not one of each. |
| `st.tabs()` unless tabs are the single best view | Avoid; tabs imply exploration, widgets present a result. |

## Full-widget examples

| Directory | What it shows | Key techniques |
|---|---|---|
| `finance_chart/` | Portfolio KPI metrics and holdings list | `st.metric`, `st.columns`, `st.markdown` with HTML |
| `data_table/` | Tabular sales report with column totals | `st.dataframe`, dict-based DataFrame construction |

## Reusable components

`components/` holds polished, atomic, copy-paste-ready `render_*`
functions — GitHub repo cards, search engine result cards, stock ticker
cards, and so on — each self-contained and scoped with its own
stylesheet.  See [`components/README.md`](components/README.md) for the
index, the copy-paste contract (no cross-file imports), and the
component-author rules.

## How to use these examples
1. Read the example closest to your target widget
2. If the task needs a rich element, read `components/README.md` and copy
   the relevant `render_*` function body + its helpers into your `app.py`
3. Adapt `app.py` (keep the `open("data.json")` pattern)
4. Build `data.json` with real data from the parent agent's tool calls
5. Write both files to the widget directory the agent prompt specifies
6. Lint: `ruff check app.py` then `python -c "import ast; ast.parse(open('app.py').read())"`
7. Call `submit_widget(widget_id=..., requirements=[...])`
