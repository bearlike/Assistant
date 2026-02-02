
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
Meeseeks is an innovative AI assistant built on a multi-agent large language model (LLM) architecture. It tackles complex problems by breaking them down into atomic, reproducible tasks, each handled by autonomous agents powered by LLMs. This approach, combined with semantic caching, significantly enhances efficiency and reduces LLM calls. It is also tested to work with OpenAI-compatible endpoints [`[*]`](https://github.com/bearlike/Personal-Assistant/wiki/Additional-Resources). Meeseeks can also call different endpoints for different tools.


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

- (✅) [LangFuse](https://github.com/langfuse/langfuse) integrations to accurate log and monitor chains.
- (✅) Use natural language to interact with integrations and tools.
- (✅) Simple REST API interface for 3rd party tools to interface with Meeseeks.
- (✅) Handles complex user queries by breaking them into actionable steps, executing these steps, and then summarizing on the results.
- (✅) Custom [Home Assistant Conversation Integration](https://www.home-assistant.io/integrations/conversation/) to allow voice assistance via [**HA Assist**](https://www.home-assistant.io/voice_control/).
- (✅) A chat Interface using `streamlit` that shows the action plan, user types, and response from the LLM.

## Extras 👽
Optional feature that users can choose to install to further optimize their experience.
- (📅) **`Quality`** Use [CRITIC reflection framework](https://arxiv.org/pdf/2305.11738) to reflect on a response to a task/query using external tools via [`[^]`](https://llamahub.ai/l/agent/llama-index-agent-introspective).
- (🚧) **`Privacy`** Integrate with [microsoft/presidio](https://github.com/microsoft/presidio) for customizable PII de-identification.

## Integrations 📦
- (✅) [Home Assistant](https://github.com/home-assistant/core)
- (🚧) Google Calendar
- (🚧) Google Search, Search recent ArXiv papers and summaries, Yahoo Finance, Yelp
- (🧐) Android Debugging Shell

## Installating and Running Meeseeks
> [!IMPORTANT]
> For Docker or manual installation, running, and configuring Meeseeks, visit [**Installation - Wiki**](https://github.com/bearlike/Personal-Assistant/wiki/Installation).

---

# Documentation 📚

This repository ships a versioned documentation site built with MkDocs + Material and Mike.

The version selector only lists versions that have been deployed with `mike` (it does not list Git branches directly).

### Local documentation workflow

1. Install documentation dependencies:

   ```bash
   pip install -r requirements-docs.txt
   ```

2. Serve docs locally:

   ```bash
   export PYTHONPATH="$PWD"
   mkdocs serve
   ```

3. Build docs locally:

   ```bash
   export PYTHONPATH="$PWD"
   mkdocs build --strict
   ```

### Deploying versioned docs with Mike

For development docs (main branch):

```bash
export PYTHONPATH="$PWD"
mike deploy dev
mike set-default dev
```

If you plan to push docs to `gh-pages` (for example, using `--push`), ensure git has a user configured:

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

For release tags (for example, `v1.2.3` -> `1.2` with `latest` alias):

```bash
export PYTHONPATH="$PWD"
mike deploy --update-aliases 1.2 latest
mike set-default latest
```

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
