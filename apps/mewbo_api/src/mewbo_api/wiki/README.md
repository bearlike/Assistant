# Wiki backend (mewbo-api[wiki])

The opt-in DeepWiki-style backend that satisfies the frontend contract in
`apps/mewbo_console/src/components/wiki/api/types.ts` and serves `/v1/wiki/*`.

## Quick-start

```bash
# Install with wiki extras
uv sync --extra wiki

# Run the API server
uv run mewbo-api
```

`/v1/wiki/*` mounts only when the wiki extras resolve. Without them the
routes are silently skipped (clean startup).

Backend env knobs:
- `MEWBO_WIKI_CLONE_ROOT` — where indexing jobs clone repos (default `/tmp/mewbo/wiki/clones`).
- `MEWBO_WIKI_SSE_MAX_IDLE` — SSE generator idle-cycle cap (default 600 cycles).
- `MEWBO_WIKI_SSE_SLEEP` — SSE generator poll interval (default 0.5s).

## Module map

```
.
├── types.py         Pydantic mirrors of api/types.ts
├── store.py         WikiStoreBase + JsonWikiStore + MongoWikiStore
├── graph.py         GraphIndex — tree-sitter (Python/JS/TS/Go/Rust)
├── embedder.py      Embedder — LiteLLM batch embeddings
├── retriever.py     HybridRetriever — BM25 + cosine + RRF + 1-hop graph
├── jobs.py          WikiIndexingJob + WikiQaSession orchestrators
├── events.py        SSE generators (poll-based)
├── routes.py        Flask-RESTX namespace — 13 endpoints
├── errors.py        WikiError → HTTP status mapping
└── catalogues.py    Platform/Language/Model lists (lifted from FE mocks)
```

## Demo repo

[git.hurricane.home/bearlike/wiki-demo](https://git.hurricane.home/bearlike/wiki-demo) —
a tiny Python pkg with a `.mewbo/wiki.json` grounder. Use it for E2E smoke
tests.

## Grounder

If the cloned repo contains `.mewbo/wiki.json` (or, for back-compat,
`.devin/wiki.json`), the indexer adopts its `pages[]` as the page plan
and injects `repo_notes[].content` into the sub-agent task prompts.

## Design + plan

- Design: `docs/specs/2026-05-14-deepwiki-style-gen-design.md`
- Plan: `docs/plans/2026-05-14-deepwiki-style-gen-plan.md`
