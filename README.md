
<h1 align="center">Meeseeks: The Personal Assistant 👋</h1>

<p align="center">
    <a href="https://github.com/bearlike/Personal-Assistant/wiki"><img alt="Wiki" src="https://img.shields.io/badge/GitHub-Wiki-blue?style=for-the-badge&logo=github"></a>
    <a href="https://github.com/features/actions"><img alt="GitHub Actions Workflow Status" src="https://img.shields.io/github/actions/workflow/status/bearlike/Personal-Assistant/docker-buildx.yml?style=for-the-badge&"></a>
    <a href="https://github.com/bearlike/Personal-Assistant/releases"><img src="https://img.shields.io/github/v/release/bearlike/Personal-Assistant?style=for-the-badge&" alt="GitHub Release"></a>
    <a href="https://github.com/bearlike/Personal-Assistant/pkgs/container/meeseeks-chat"><img src="https://img.shields.io/badge/ghcr.io-bearlike/meeseeks--chat:latest-blue?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Image"></a>
    <a href="https://github.com/bearlike/Personal-Assistant/pkgs/container/meeseeks-api"><img src="https://img.shields.io/badge/ghcr.io-bearlike/meeseeks--api:latest-blue?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Image"></a>
</p>



> Look at me, I'm Mr Meeseeks.


<p align="center">
    <img src="docs/screenshot_chat_app_1.png" alt="Screenshot of Meeseks WebUI" height="512px">
</p>

# Project Motivation 🚀
Meeseeks is a personal assistant built on an LLM-driven orchestration loop. It breaks a request into atomic steps, runs tools, and returns a clean summary. The core loop can replan after tool failures and keep short-term state while sessions persist on disk for continuity.


<details>
<summary><i>Legends (Expand to View) </i></summary>

| Completed | In-Progress | Planned | Scoping |
| :-------: | :---------: | :-----: | :-----: |
|     ✅    |     🚧     |    📅   |    🧐   |

</details>

# Features 🔥
> [!NOTE]
> Visit [**Features - Wiki**](https://github.com/bearlike/Personal-Assistant/wiki/Features) for detailed information on tools and integration capabilities.

<table align="center">
    <tr>
        <th>Answer questions and interpret sensor information</th>
        <th>Control devices and entities</th>
    </tr>
    <tr>
        <td align="center"><img src="docs/screenshot_ha_assist_1.png" alt="Screenshot" height="512px"></td>
        <td align="center"><img src="docs/screenshot_ha_assist_2.png" alt="Screenshot" height="512px"></td>
    </tr>
</table>

- (✅) Optional [LangFuse](https://github.com/langfuse/langfuse) integrations for observability (auto-disabled if not configured).
- (✅) Use natural language to interact with integrations and tools.
- (✅) Simple REST API interface for 3rd party tools to interface with Meeseeks.
- (✅) Handles complex user queries by breaking them into actionable steps, executing these steps, and then summarizing on the results.
- (✅) Optional [Home Assistant Conversation Integration](https://www.home-assistant.io/integrations/conversation/) to allow voice assistance via [**HA Assist**](https://www.home-assistant.io/voice_control/).
- (✅) A chat Interface using `streamlit` that shows the action plan, user types, and response from the LLM.
- (✅) Terminal CLI for interactive sessions with plan + tool result visibility.
- (✅) MCP tool visibility and model switch wizard in the CLI.
- (✅) Plan -> act -> observe loop with re-planning on tool failures.
- (✅) Session transcripts with lightweight compaction for long-running chats.
- (✅) Tool registry with optional MCP tool support via manifest.
- (✅) Permission gate + hooks around tool execution for safer runs.

## Extras 👽
Optional feature that users can choose to install to further optimize their experience.
- (📅) **`Quality`** Use [CRITIC reflection framework](https://arxiv.org/pdf/2305.11738) to reflect on a response to a task/query using external tools via [`[^]`](https://llamahub.ai/l/agent/llama-index-agent-introspective).
- (🚧) **`Privacy`** Integrate with [microsoft/presidio](https://github.com/microsoft/presidio) for customizable PII de-identification.

## Integrations 📦
- (✅) [Home Assistant](https://github.com/home-assistant/core)
- (🚧) Google Calendar
- (🚧) Google Search, Search recent ArXiv papers and summaries, Yahoo Finance, Yelp
- (🧐) Android Debugging Shell

## Monorepo layout 🧭
- `core/`: orchestration loop, schemas, session storage, compaction, tool registry.
- `tools/`: tool implementations and integrations (including Home Assistant and MCP).
- `meeseeks-api/`: Flask REST API for programmatic access.
- `meeseeks-chat/`: Streamlit UI for interactive chat.
- `meeseeks-cli/`: Terminal CLI frontend for interactive sessions.
- `meeseeks_ha_conversation/`: Home Assistant integration that routes voice to the API.
- `prompts/`: planner prompt and examples.

## Architecture (short) 🧩
Requests flow through a single core engine used by every interface, so behavior stays consistent across UI, API, and voice.

```mermaid
flowchart LR
  User --> Chat
  User --> API
  HA --> API
  User --> CLI
  Chat --> Core
  API --> Core
  CLI --> Core
  Core --> Tools
  Tools --> HomeAssistant
  Tools --> MCP
  Core --> SessionStore
```

## Installing and Running Meeseeks
> [!IMPORTANT]
> For Docker or manual installation, running, and configuring Meeseeks, visit [**Installation - Wiki**](https://github.com/bearlike/Personal-Assistant/wiki/Installation) or read `docs/README.md` for a quick local/deploy overview.

## MCP servers (quick setup)
MCP tools are auto-discovered from your MCP server config.
- Copy `configs/mcp.example.json` and set the MCP server URL + headers.
- Set `MESEEKS_MCP_CONFIG` in `.env`.
- First run will auto-generate the tool manifest under `~/.meeseeks/` and refresh it on each load.
- Optional: add `auto_approve_tools` under each server to allowlist tools (the CLI writes this when you pick “Yes, always”).

Advanced: set `MESEEKS_TOOL_MANIFEST` to override the tool list (disables auto-discovery).
Tip: if you override the manifest, include `talk_to_user_tool` so the assistant can still reply.

## Optional components
- **Langfuse** is optional. Enable it by setting `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` (or disable with `LANGFUSE_ENABLED=0`).
- **Home Assistant** tools are optional. Enable them by setting `HA_URL` and `HA_TOKEN` (or disable with `MESEEKS_HOME_ASSISTANT_ENABLED=0`).
- Optional components auto-disable when init/runtime/auth fails, with logs explaining the reason.

---

# Contributing 👏

We welcome contributions from the community to help improve Meeseeks. Whether you want to fix a bug, add a new feature, or integrate a new tool, your contributions are highly appreciated.

To contribute to Meeseeks, please follow these steps:

1. Fork the repository and clone it to your local machine.
2. Create a new branch for your contribution.
3. Make your changes, commit your changes and push them to your forked repository.
4. Open a pull request to the main repository, describing your changes and the problem they solve.

## Bug Reports and Feature Requests 🐞

If you encounter any bugs or have ideas for new features, please open an issue on our [issue tracker](https://github.com/bearlike/Personal-Assistant/issues). We appreciate detailed bug reports that include steps to reproduce the issue and any relevant error messages.

Thank you for considering contributing to Meeseeks! Let's build cool stuff!
