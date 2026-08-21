# App Examples

Read this file first, then explore the subdirectories for working stlite app code.

## What an app is (vs a widget)

A **widget** is one focused result card rendered inline in a chat message. An **app**
is a durable, first-class thing: a multi-page stlite frontend, one or more
schema-validated data collections, and the pipelines that keep them fresh. An app
has a data model; a widget just displays data it was handed.

Because of that, the rules differ from the widget builder:

- **An app is a page, not a card.** `st.header` / `st.subheader` / `st.sidebar` /
  `st.tabs` are all allowed — multi-section layout and page navigation are the point.
- **Data comes from the SDK, not a bundled `data.json`.** Every app bundle is
  injected with `mewbo_app` (see the top-level `sdk/mewbo_app.py`) plus a render
  context; app code reads live data through `mewbo_app.connect().data.query(...)`.
  **Never hand-write `requests`/`httpx`/`urllib`/`js` glue — the SDK is the only
  network path**, and the submit-time lint gate enforces it.
- **`st.set_page_config()` is still banned** — the console/WebView host owns stlite
  config, app or widget.
- **Documents must be display-ready.** v1 apps are read-only: the frontend cannot
  call the agent. A pipeline's job is to leave each collection in a shape the UI can
  render directly (denormalized, sorted, labelled). Do not push aggregation into the
  frontend.

## The reference app

| Directory | What it shows | Key techniques |
|---|---|---|
| `email_organizer/` | A multi-page inbox-organizer: a dashboard of task groups + an all-mail page | `mewbo_app.connect()`, `app.data.query`, `app.system.runs`, `pages/`, `AppTokenExpired` handling |

`email_organizer/SUBMIT.md` shows the exact `submit_app` call — the two collection
schemas and the cron pipeline `wake_prompt` — that turns those files into a live app.

## Pipeline cookbook

`recipes/` is a short scenario cookbook for server-side code pipelines. Each directory pairs a
manifest fragment with runnable pipeline source. Read the closest scenario before inventing a
pipeline shape; every recipe declares samples and an output contract so a bad assumption reaches
the submit-time check instead of becoming a green, empty run.

| Request shape | Start with |
|---|---|
| Fetch current CLI data whenever the app reads | `recipes/cli-json/` or `recipes/cli-text/` (`tier="render"`) |
| Rebuild stored data when workspace files change | `recipes/files-to-collection/` (`tier="materialize"`, `cache_mode="source"`) |
| Classify or summarize source text | `recipes/llm-transform/` (bounded `ctx.llm`) |
| Let a served app submit a form | `recipes/user-input/` (`user_writable`) |
| Check a relation JSON Schema cannot express | `recipes/verifier/` (`verify(result, ctx)`) |

Choose `cli-json` only when the CLI actually emits JSON; use `cli-text` when it does not. Choose a
live render pipeline for current, read-time data and a materialize pipeline for a durable snapshot.

## Reusable components

`components/` holds copy-paste page components (a data table, a metric header, a
freshness strip). Each is one self-contained class with a `render()` method, scoped
between `# ── <ClassName> ─` and `# ── end <ClassName> ─` markers. Extract the block
you want and paste it into your frontend — no cross-file imports.

## Linting

App files are linted at submit time by `plugin/linter.py` (the authority) and,
statically here, by `ruff.toml` in this folder. The allowlist is the widget
allowlist plus `mewbo_app`; every raw network module and dynamic-execution call is
banned.
