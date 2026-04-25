
<p align="center">
  <img src="docs/logos/logo-transparent.svg" alt="Mewbo logo" width="96" />
</p>

<h1 align="center">Mewbo</h1>
<p align="center"><em>Open-source, self-hosted AI orchestration system for long-horizon work. Parallel sub-agents you can scope, observe, and steer, with durable context and provider-agnostic model routing.</em></p>

<p align="center">
    <a href="https://deepwiki.com/bearlike/Assistant"><img alt="Ask DeepWiki" src="https://deepwiki.com/badge.svg"></a>
    <a href="https://github.com/bearlike/Assistant/actions/workflows/docker-buildx.yml"><img alt="Build and Push Docker Images" src="https://github.com/bearlike/Assistant/actions/workflows/docker-buildx.yml/badge.svg"></a>
    <a href="https://github.com/bearlike/Assistant/actions/workflows/lint.yml"><img alt="Lint" src="https://github.com/bearlike/Assistant/actions/workflows/lint.yml/badge.svg"></a>
    <a href="https://github.com/bearlike/Assistant/actions/workflows/docs.yml"><img alt="Docs" src="https://github.com/bearlike/Assistant/actions/workflows/docs.yml/badge.svg"></a>
    <a href="https://codecov.io/gh/bearlike/Assistant"><img src="https://codecov.io/gh/bearlike/Assistant/graph/badge.svg?token=OJ2YUCIZ2I" alt="Codecov"></a>
    <a href="https://github.com/bearlike/Assistant/releases"><img src="https://img.shields.io/github/v/release/bearlike/Assistant" alt="GitHub Release"></a>
    <a href="https://github.com/bearlike/Assistant/pkgs/container/mewbo-api"><img src="https://img.shields.io/badge/ghcr.io-bearlike/mewbo--api:latest-blue?logo=docker&logoColor=white" alt="Docker Image"></a>
</p>



https://github.com/user-attachments/assets/78754e8f-828a-4c54-9e97-29cbeacbc3bc

<table align="center">
    <tr>
        <td align="center"><img src="docs/mewbo-console-02-tasks.png" alt="Mewbo task detail page" height="320px"></td>
        <td align="center"><img src="docs/mewbo-console-01-front.png" alt="Mewbo console landing page" height="320px"></td>
    </tr>
    <tr>
        <td align="center"><img src="docs/mewbo-console-03-plan-approval.jpg" alt="Plan approval in the Mewbo console" height="280px"></td>
        <td align="center"><img src="docs/mewbo-console-04-file-edit.jpg" alt="File-edit diff card in the Mewbo console" height="280px"></td>
    </tr>
    <tr>
        <td align="center"><img src="docs/mewbo-console-05-plugins.png" alt="Plugins page with installed plugins and marketplace listings" height="280px"></td>
        <td align="center"><img src="docs/screenshot_ha_assist_2.png" alt="Home Assistant device control" height="280px"></td>
    </tr>
    <tr>
        <td colspan="2" align="center"><img src="docs/mewbo-console-07-widgets.png" alt="Stock ticker and GitHub repo card widgets rendered inline in the Mewbo Console" width="100%"></td>
    </tr>
    <tr>
        <td colspan="2" align="center"><img src="docs/mewbo-email-01.jpg" alt="Mewbo email thread in Gmail" height="400px"></td>
    </tr>
</table>

## Overview

Mewbo is an open-source, self-hosted AI orchestration system for long-horizon work. Tasks decompose into parallel sub-agents that each carry only the tools they need, exchange compressed summaries instead of raw transcripts, and run within resource budgets you tune per deployment. You approve destructive actions, watch the agent tree as it grows, and can interrupt or steer any branch mid-flight. Sessions persist with full provenance, compact automatically near the budget, and survive across every client.

## Features

- **Agent hypervisor.** Sub-agents spawn in parallel with scoped tools and approval-gated actions. Progress shows as a live tree, and you can steer or cancel any branch mid-flight. The hypervisor enforces resource budgets through natural-language warnings rather than force-kills, and resolves every child into a structured result.
- **Long-horizon context.** Two-mode compaction summarises older turns near the budget. Post-compact file restoration replays the working set. Conversation fork lets you branch from any message and replay against a different model.
- **Native skills, plugins, and MCP.** Agent Skills, plugins from any compatible marketplace, and MCP servers load from user or project scope without translation. Plugins also contribute per-session stateful tools, hooks, and agent definitions.
- **Inline interactive widgets.** Sub-agents author Streamlit-in-WASM widgets that mount in a sandboxed Web Worker inside the conversation, with no server round-trip and no CORS.
- **Provider-agnostic, multi-surface.** Any model behind LiteLLM, accessed from a terminal CLI, web console, REST API, Home Assistant, Nextcloud Talk, or email. Same session, same tools, same transcript.

## Get started

See [docs.mewbo.com/getting-started](https://docs.mewbo.com/getting-started/) to install Mewbo and run a first session.

## Documentation

Full documentation lives at **[docs.mewbo.com](https://docs.mewbo.com/)**.

| Section | Covers |
| --- | --- |
| [Get Started](https://docs.mewbo.com/getting-started/) | Install, configure an LLM, run a first session. |
| [Configure](https://docs.mewbo.com/configuration/) | LLM setup, project config, configuration reference. |
| [Clients](https://docs.mewbo.com/clients-cli/) | CLI, web console, REST API, Home Assistant, Nextcloud Talk, email. |
| [Capabilities](https://docs.mewbo.com/features-builtin-tools/) | Built-in tools, sub-agents, skills, plugins, widgets, plan mode, permissions, compaction. |
| [Deploy](https://docs.mewbo.com/deployment-docker/) | Docker Compose, storage backends, production setup. |
| [Develop](https://docs.mewbo.com/core-orchestration/) | Architecture, session runtime, building a client, API reference. |
| [Releases](https://github.com/bearlike/Assistant/releases) | Release notes and upgrade history. |

## Contributing

Bugs and feature requests on the [issue tracker](https://github.com/bearlike/Assistant/issues). For development setup, see the [developer guide](https://docs.mewbo.com/developer-guide/).

## License

[MIT](LICENSE) © Krishnakanth Alagiri.
