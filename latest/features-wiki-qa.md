# Question Answering

Every wiki page carries an inline Q&A box: **Ask MewboWiki**. Ask a question about the repository, pick a model, and a coordinating agent answers from the same graph that backs the pages. The answer is fast, authoritative, and grounded in the code itself.

<video controls preload="metadata" style="width: 100%; max-width: 960px; height: auto; display: block; margin: 2rem auto 0;">
  <source src="../assets/videos/mewbo-wiki-qna-demo.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-wiki-08-qna.jpg" alt="An Ask MewboWiki answer to 'What is this project for?' generated with claude-sonnet-4-6: a left rail with a Summary card, an expandable Cited Sources list (project overview, agentic search engine, source capability graph, channels & integrations, API server), and a Retrieval details panel listing the pages accessed and the model used; the answer on the right describes the project's core capabilities and repository structure with inline wiki citation chips, and a follow-up question box sits at the bottom" style="width: 100%; max-width: 960px; height: auto;" />
</div>

---

## How Ask MewboWiki builds answers

When you submit a question, a coordinating agent fans out several **probe agents** in parallel. Each probe explores a different angle of the codebase using the [knowledge graph](features-wiki-graph.md). ANN-guided entry points steer each probe toward the most relevant symbols and notes. The coordinator collects their findings and synthesises them into one answer.

The fan-out is bounded: if the step budget is exhausted, the coordinator wraps up with whatever was found rather than running indefinitely. Cross-module questions benefit most; the fan-out naturally pulls together evidence that a single-agent search would miss.

> [!NOTE] Why multi-hop matters
> A flat search answers a cross-module question with three disconnected results. Traversing the memory graph follows real relationships in the code instead, so the answer holds together and every hop stays grounded. The mechanics live on [The Knowledge Graph](features-wiki-graph.md#grounded-by-a-code-memory-graph).

---

## Grounded, cited answers

Every answer is traceable back to the code it came from:

- **Inline citation chips.** `[path:line-range]` markers are embedded directly in the answer text. Click a chip to expand a **source card** showing the actual code excerpt from that file, so you can verify every claim without leaving the wiki.
- **A Sources panel.** Below the answer, a **Cited Sources** list and a **Retrieval details** panel enumerate every wiki page and source file that was accessed during generation, alongside the model that wrote the answer. Nothing is hidden behind the prose.

---

## Answers that teach the wiki

Ask MewboWiki doesn't just read the memory layer. It grows it. When an answer turns up a durable fact (something worth remembering), Mewbo **automatically deposits a note** into the memory layer: validated, condensed, anchored to the right symbols, and merged off the critical path so it doesn't slow the answer down. Every future answer is built on the accumulated set. → [The memory grows as the wiki is used](features-wiki-graph.md#the-memory-grows-as-the-wiki-is-used)

---

## Ask from your agent fleet

The same Q&A is exposed to external agents over the [MCP server](clients-mcp.md). Any agent on your fleet (Claude Code, Codex, Cursor, or another Mewbo) can query an indexed project with the authenticated `ask_wiki` tool and get back the same cited answer, without opening the console:

- **`ask_wiki`**: ask a natural-language question about an indexed project and get a cited answer. Long questions return an `answer_id` with `status: "running"`.
- **`get_wiki_answer`**: resume or replay an answer by its `answer_id` once it settles.
- **`read_wiki_structure`** / **`read_wiki_page`** / **`list_wiki_projects`**: browse the graph structure and pages directly.
- **`submit_insight`**: teach the wiki a durable fact your agents discovered while working. → [The memory grows as the wiki is used](features-wiki-graph.md#the-memory-grows-as-the-wiki-is-used)

The same operations are available on the REST API under the `/v1/wiki/*` routes. See the [MCP server](clients-mcp.md) page for the full tool list and the [REST API reference](rest-api.md) for the HTTP surface.
