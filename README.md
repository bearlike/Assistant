
<h1 align="center">Meeseeks: The Personal Assistant 👋</h1>

> Look at me, I'm Mr Meeseeks.


<p align="center">
    <img src="docs/screenshot_chat_app_1.png" alt="Screenshot of Meeseks WebUI" height="512px">
</p>

## Project Motivation 🚀
Meeseeks is an AI assistant powered by a multi-agent large language model (LLM) architecture that breaks down complex problems into smaller, more manageable tasks. These tasks are then handled by autonomous agents, which leverage the reasoning capabilities of LLMs. By decomposing problems into smaller queries, Meeseeks significantly improves caching efficiency, especially when combined with semantic caching techniques.

The project takes advantage of various models through OpenAI-compatible endpoints (Lite-LLM, vLLM, ollama text-generation-ui, etc.) to interact with various self-hosted and cloud inference models. This approach allows users to benefit from privacy by using local models ([`microsoft/phi-3-mini-128k-instruct`](https://huggingface.co/microsoft/Phi-3-mini-128k-instruct), [`meta-llama/Meta-Llama-3-8B`](https://huggingface.co/meta-llama/Meta-Llama-3-8B), etc) and performance of cloud inference models (`anthropic/claude-3-opus`, `openai/gpt-4-turbo`, etc)

Meeseeks builds upon recent advancements in LLM-based multi-agent systems, which have shown promising results in collaborative problem-solving, RAG, consensus-seeking, and theory of mind inference in cooperative settings. By harnessing the planning and decision-making capabilities of LLMs, Meeseeks should provide an efficient and effective solution for handling complex tasks across various tools.

## Features 🔥
<details>
<summary><i>Legends (Expand to View) </i></summary>

| Completed | In-Progress | Planned | Scoping |
| :-------: | :---------: | :-----: | :-----: |
|     ✅    |    🚧      |   📅    |    🧐   |

</details>



| Status | Feature                                                                                                                                                                                                  |
| :----: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|   ✅    | [LangFuse](https://github.com/langfuse/langfuse) integrations to accurate log and monitor chains.                                                                                                       |
|   ✅    | Use natural language to interact with integrations and tools.                                                                                                                                           |
|   🚧    | Simple REST API interface for interfacing with the system. Used by the the `HA Conversation Integration`.                                                                                               |
|   ✅    | Decomposes user queries to a `TaskQueue` with a list of `ActionSteps`.                                                                                                                                  |
|   🚧    | Custom [Home Assistant Conversation Integration](https://www.home-assistant.io/integrations/conversation/) to allow voice assistance via [**HA Assist**](https://www.home-assistant.io/voice_control/). |
|   🧐    | **(Extras - Quality)** Use CRITIC reflection framework to reflect on a response to a task/query using external tools via `llama_index.agent.introspective.ToolInteractiveReflectionAgentWorker`.        |
|   📅    | **(Extras - Privacy)** Integrate with [microsoft/presidio](https://github.com/microsoft/presidio) for customizable PII de-identification.                                                               |
|   ✅    | A chat Interface using `streamlit` that shows the action plan, user types, and response from the LLM.                                                                                                   |

### Integrations 📦

| Status  |             Integration Name             | Description                                                                                                                       |
| :----:  | :--------------------------------------: | --------------------------------------------------------------------------------------------------------------------------------- |
|   ✅    |  [Home Assistant](https://github.com/home-assistant/core)    | Control devices and retrieve sensor data via `request`                                                                            |
|   📅    |                  Gmail                   | Access Gmail functionality via `llama_index.tools.google.GmailToolSpec`                                                           |
|   🚧    |             Google Calendar              | Access Google Calendar functionality via `llama_index.tools.google.GoogleCalendarToolSpec`                                        |
|   📅    |              Google Search               | Perform Google search via `llama_index.tools.google.GoogleSearchToolSpec`                                                         |
|   📅    | Search recent ArXiv papers and summaries | Search and retrieve summaries of recent ArXiv papers via `llama_index.tools.arxiv.ArxivToolSpec`                                  |
|   📅    |              Yahoo Finance               | Access stock, news, and financial data of a company from Yahoo Finance via `llama_index.tools.yahoo_finance.YahooFinanceToolSpec` |
|   📅    |                   Yelp                   | Search for businesses and fetch reviews from Yelp via `llama_index.tools.yelp.YelpToolSpec`                                       |
|   🧐    |         Android Debugging Shell          | Use Home Assistant ADB Integrations to perform actions on Android TV Variants via `integrations.HomeAssistant`.                   |


## Running Meeseeks with Docker 🐳 (Recommended)

1. **Install Docker**: If you haven't already, [install Docker on your machine](https://docs.docker.com/get-docker/).

2. **Environment Variables**: Copy the [`.env.example`](".env.example") file to a new file named ``docker-meeseeks.env`` and modify the necessary environment variables.

3. **Run the Docker Container**: Default port is `8502`. Run the Docker container with the following command.

```bash
docker run --env-file docker-meeseeks.env -p 8502:8502 ghcr.io/bearlike/meeseeks-chat:latest
```
4. Now, you should be able to access the Meeseeks chat interface at `http://hostname-or-ip:8502` in your web browser. Enjoy chatting with Meeseeks!

## Project Setup 🛠️

This project is composed of multiple modules, each with its own set of dependencies. Follow the steps below to set up the project on your local machine.

### Prerequisites

- Python 3.10 or higher
- [Poetry](https://python-poetry.org/docs/#installation) - Simplifies managing Python packages and dependencies.


> [!NOTE]
>  **Installation script for Mac and Linux Users:** Clone the repository, navigate to the project root and run the below script:
> ```bash
> chmod +x build-install.sh
> ./build-install.sh fallback-install
> ```


### Manual Installation

1. Clone the repository:

    ```bash
    git clone https://github.com/bearlike/Personal-Assistant.git
    ```

2. Navigate to the root directory of the project and install the main module's dependencies:

    ```bash
    cd Personal-Assistant
    poetry install
    ```

3. To install the optional and development dependencies, navigate to the submodule directories (`meeseeks-api` and `meeseeks-chat`), and install their dependencies:

    ```bash
    poetry install --dev
    cd ../meeseeks-api
    poetry install
    cd ../meeseeks-chat
    poetry install
    cd ..
    ```

4. **Environment Variables**: Copy the [``.env.example``](.env.example) file to a new file named ``.env`` and modify in the necessary environment variables.


## Running the Meeseeks Chat Interface 🤖

After installing the dependencies, you can run the application with the following command:

```bash
cd ./meeseeks-chat
streamlit run chat_master.py
```


---

## Contributing 👏

We welcome contributions from the community to help improve Meeseeks. Whether you want to fix a bug, add a new feature, or integrate a new tool, your contributions are highly appreciated.

To contribute to Meeseeks, please follow these steps:

1. Fork the repository and clone it to your local machine.
2. Create a new branch for your contribution.
3. Make your changes, commit your changes and push them to your forked repository.
4. Open a pull request to the main repository, describing your changes and the problem they solve.

## Bug Reports and Feature Requests 🐞

If you encounter any bugs or have ideas for new features, please open an issue on our [issue tracker](https://github.com/bearlike/Personal-Assistant/issues). We appreciate detailed bug reports that include steps to reproduce the issue and any relevant error messages.

Thank you for considering contributing to Meeseeks! Let's build cool stuff!


Thank you for considering contributing to Meeseeks! Let's build cool stuff!

## Additional Resources 📚
- [vllm-project/vLLM](https://github.com/vllm-project/vllm): A high-throughput and memory-efficient inference and serving engine for LLMs
- [BerriAI/Lite-LLM](https://github.com/BerriAI/litellm): Call 100+ LLM APIs using the OpenAI API format.
- [ollama/ollama](https://github.com/ollama/ollama): Get up and running with Llama 3, Mistral, Gemma, and other large language models locally.
- [oobabooga/text-generation-webui](https://github.com/oobabooga/text-generation-webui): A Gradio web UI for Large Language Models. Supports transformers, GPTQ, AWQ, EXL2, llama.cpp (GGUF), Llama models.


