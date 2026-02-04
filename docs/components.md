# Components

This repository is a monorepo. Each component lives in its own folder:

- `packages/meeseeks_core/`: orchestration loop, schemas, session storage, compaction, tool registry.
- `packages/meeseeks_tools/`: tool implementations and integration glue.
- `apps/meeseeks_api/`: Flask API that exposes the assistant over HTTP.
- `apps/meeseeks_chat/`: Streamlit UI for interactive chat.
- `apps/meeseeks_cli/`: terminal CLI for interactive sessions.
- `meeseeks_ha_conversation/`: Home Assistant integration that routes voice requests to the API.

Prompts now live under `packages/meeseeks_core/src/meeseeks_core/prompts/` and are packaged with the core module.
