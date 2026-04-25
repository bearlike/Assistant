# Widget component library

Atomic, copy-paste reusable Streamlit components for stlite widgets.
Each file packages **one** component as a single class whose state is
held in instance variables and whose rendering lives in one `render()`
method — the same shape for every component so the sub-agent only has
to learn it once.

## Two rules (non-negotiable, for agents and humans alike)

### 1. A widget is ONE atomic component, not a page

The widget sits **inline in a chat message** — not a standalone
Streamlit app. Output exactly one bordered card (or one loop rendering
N cards of the same kind) inside a single `st.container(border=True)`.

**Don't** build multi-section pages with `st.header("Stats")` +
`st.subheader("Commits")` + `st.divider()` + separate detail panels.
Those are dashboards, not widgets. They fill the conversation pane,
force scrolling, and dilute the one thing the user asked to see.

| Right | Wrong |
|---|---|
| `GitHubRepoCard(repo).render()` | `st.header("Repository")` + card + `st.subheader("Open Issues")` + another card + `st.subheader("Recent Commits")` + dataframe |
| `for r in results: SearchResultCard(r).render()` | three cards in a row followed by a paginator and a filter bar |
| One `st.metric(chart_data=…)` that shows price + delta + sparkline in one primitive | header + separate big-text component + separate chart block + separate stats row |

If the task seems to call for multiple sections, ask: "is this really
one thing, or three things?". If three, pick the one the user most
cares about and make a tight card for it; surface the rest as follow-up
actions, not crammed chrome.

### 2. Streamlit-native first

Before reaching for raw HTML, `unsafe_allow_html=True`, or custom CSS,
compose from the primitives Streamlit already ships. They keep the
visual style consistent across widgets and are dramatically easier for
a model to reproduce faithfully.

| Need | Native primitive |
|---|---|
| Bordered card frame | `st.container(border=True)` |
| Big bold number + colored delta + sparkline | `st.metric(label, value, delta=…, chart_data=[…], chart_type="area")` |
| Layout row / grid | `st.columns(spec, vertical_alignment="center", gap="medium")` |
| Muted footer / metadata | `st.caption("Updated Dec 1, 2024 · MIT")` |
| Colored tag / pill | inline `:blue-badge[python]` / `:gray-badge[MIT]` in markdown |
| Colored inline text | `:red[-2.13%]` / `:green[+1.2%]` |
| Icons | `:material/star:` / emoji |
| Thumbnail / avatar | `st.image(url, width="stretch")` |
| CTA link | `st.link_button(label, url, type="tertiary", icon=":material/open_in_new:")` |

Custom CSS is a last resort, not a starting point. If you find yourself
writing a `<style>` block, step back and look for the primitive you
missed.

## File structure

```
# ── <ClassName> ─────────────────────────────────────────────────────

class <ComponentState>(TypedDict, total=False): ...
class <ClassName>:
    def __init__(self, state): self.state = state
    def render(self) -> None: ...   # one `st.container(border=True)` inside

# ── end <ClassName> ─────────────────────────────────────────────────
```

The two banner comments bracket the entire extractable block (TypedDict
+ class). Everything the widget needs is inside. Everything outside —
demo payloads, `__main__` preview — stays behind.

## Extract a component in one command

```bash
awk '/^# ── GitHubRepoCard ─/,/^# ── end GitHubRepoCard ─/' \
    "$CLAUDE_PLUGIN_ROOT/examples/components/github_repo_card.py" \
    >> app.py
```

Pipe into your widget's `app.py` (append, don't replace — you can stack
extractions). Class names are unique across the library; stacking never
collides.

## Assembly example — one atomic widget

```python
# app.py (assembled from one extraction + data)
import json
import streamlit as st
from typing import TypedDict

# ── SearchResultCard ──────── (pasted block)
class SearchResult(TypedDict, total=False): ...
class SearchResultCard: ...
# ── end SearchResultCard ──

with open("data.json") as f:
    data = json.load(f)

# Widget body is ONE loop of ONE card type — not a page with sections.
for hit in data["results"]:
    SearchResultCard(hit).render()
```

## Calling convention

```python
ComponentCard(state_dict).render()
```

- `state_dict`: the typed state payload (`GitHubRepo`, `SearchResult`, `StockTick`).
- No keyword knobs by default — the component picks sensible defaults. If a
  specific card class needs an option (e.g. `show_sparkline=False`), the
  class will expose it as a keyword-only argument, documented on the class.

## Author contract — adding new components

Every new component file **must**:

| Rule | Why |
|---|---|
| Declare a banner pair (`# ── <ClassName> ─` / `# ── end <ClassName> ─`) wrapping the TypedDict + class | The `awk` extraction idiom depends on it |
| Import only from `../linter.py::ALLOWED_MODULES` | The widget that copies this code is linted at submit time |
| Single public class, unique name across the library, ending in `Card` | Multiple components must coexist in one widget without collision |
| Render exactly one `st.container(border=True)` | Rule #1: one atomic component per widget card |
| Compose from Streamlit primitives — see the "Streamlit-native first" table above | Rule #2: visual consistency |
| End with `if __name__ == "__main__":` demo rendering each canonical payload | `streamlit run <file>.py` preview |
| Fit in roughly 150 lines | Above that, the class is doing too much — split it or cut features |

**Must not**: read from network / shell / filesystem, depend on sibling
component files, use `components.v1.declare_component`, use
`time.sleep()`, or inject custom HTML when a primitive exists.

## Available components

| File | Class | State | What it renders |
|---|---|---|---|
| `github_repo_card.py` | `GitHubRepoCard` | `GitHubRepo` | Title, description, stars/forks/language as `st.metric`, topic chips via inline badges, license + updated footer |
| `search_result_card.py` | `SearchResultCard` | `SearchResult` | Favicon/source/date caption, title link, snippet, optional right-column thumbnail, badge + author footer |
| `stock_ticker_card.py` | `StockTickerCard` | `StockTick` | Symbol + name header, `st.metric(chart_data=…, chart_type="area")` with embedded sparkline, stats row (day high/low, volume, market cap) |
| `plantuml_card.py` | `PlantUMLCard` | `PlantUMLDiagram` | Any PlantUML diagram type (sequence, class, component, …) fetched as SVG from a public or self-hosted server; `server_url` field lets you swap the backend |

## Extending the library

Adding a component = one new file that follows the banner + class
pattern above + one row in the table. No code changes elsewhere. If
your component needs a new third-party library, append it to
`../linter.py::ALLOWED_MODULES` **and** to the "Allowed libraries"
list in `../../agents/st-widget-builder.md` — the parity test enforces
both sides stay in sync.
