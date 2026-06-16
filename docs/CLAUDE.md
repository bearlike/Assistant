> ↑ [root /CLAUDE.md](../CLAUDE.md)

# Documentation site — authoring guide

Scope: `docs/` — the MkDocs site (`mkdocs.yml`, theme `shadcn`/mkdocs-shadcn-mewbo). The REST reference is Scalar over `docs/openapi.json` (see [root → "REST API reference"]); most pages are hand-authored markdown.

## Code-reference badges — link to what we own

**Rule (KISS/DRY):** any reference to an artifact **we own** renders as a badge, never a bare hyperlink or inline code. Two kinds, and *only* these two:

1. **A file/artifact in our repo** (`bearlike/Assistant`, GitHub or the Gitea mirror):
   `[`backend.py`](repo:apps/mewbo_api/src/mewbo_api/backend.py)` — optional line range `…#L12-L20`.
2. **One of our REST endpoints** (`/api/...` or `/v1/...`):
   `[POST /api/sessions](endpoint:POST /api/sessions)` — method optional.

You write a normal markdown link with a `repo:` or `endpoint:` URI; the MkDocs hook `docs/hooks/code_refs.py` rewrites it to the badge at build time (file badge = GitHub octicon + path, SHA-pinned to the build commit; endpoint badge = method-tinted, deep-linked into the Scalar reference). Styling lives in `docs/assets/code-refs.css` (bound to the theme's design tokens, light + dark). **Never hand-write badge HTML** — just use the scheme.

### Do NOT badge (leave as plain inline `code`)
- **Anything we don't own:** third-party API endpoints (Gitea/GitHub `/repos/...`, `/api/v1/users/...`; LiteLLM `/v1/models`), external URLs, package names.
- **Config keys / settings** (`api.master_token`, `agent.lsp.enabled`), env vars, CLI commands, tool/event/field names, JSON values, MIME types.
- **`configs/app.json`** — user-created and gitignored, so it isn't a committed artifact (`configs/app.schema.json` and `configs/app.example.json` *are* → badge those).
- **Headings** (`#…`) and **fenced code blocks** (```` ``` ````) — badges there break the TOC/anchors or the code sample.

### Guardrails
- A `repo:` path must be **repo-root-relative and exist** (verify before adding). Don't fabricate line numbers — omit the range and link the whole file unless you've confirmed the lines.
- Minimal edits: swap the reference token to a badge link; don't rewrite prose.
- The generated `configuration.md` (from `docs/hooks/schema_to_md.py`) emits the same `repo:` scheme — keep new generators consistent.

## Build & gotchas
- `make docs-build` = `mkdocs build --strict`. The badge hook runs `on_page_content` (rendered HTML), so refs inside code fences survive untouched.
- A backtick link label (`` [`x.py`](repo:…) ``) renders to `<code>…</code>` inside the anchor — the hook strips inline tags to plain text; don't rely on the wrapper.
- `docs/hooks/` is in `exclude_docs` so build scripts aren't published.
- `configuration.md` and `openapi.json` are **generated** — edit the generators (`schema_to_md.py`, `scripts/ci/generate_openapi_spec.py`), not the output.
