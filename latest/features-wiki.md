# Agentic Wiki

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-wiki-01-landing.jpg" alt="The Agentic Wiki landing page in the Mewbo Console: a repository URL field with a Generate Wiki button, a resumable 'Incomplete indexes' notice, and cards for already-indexed projects (acme/beacon, bearlike/Assistant, bearlike/Grove) showing their host, branch, and page counts" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Paste a repository URL and Mewbo writes the documentation for it. The **Agentic Wiki** indexes a codebase, maps its structure into a code memory graph, and generates a navigable, grounded wiki. Every page is backed by the source files it describes. Ask a question about the repo and a sub-agent answers from that same graph: fast, authoritative, and grounded in the code itself.

A generated wiki is branded **MewboWiki** inside the product.

---

## What you get

A generated wiki is a full documentation site for one repository:

- **An overview that reads like docs, not a file dump.** Prose pages explain the runtime model, architecture, and conventions, with flow diagrams and an *On this page* outline. Each page lists the *Relevant source files* it was written from, so every claim traces back to code.
- **An interactive knowledge graph.** The whole repository as an interactive 3D graph spanning three layers: structural symbols, abstract entities, and memory notes. → [The Knowledge Graph](features-wiki-graph.md)
- **Ask MewboWiki.** An inline Q&A box on every page. Ask a question, pick a model, and a coordinating agent fans out probe agents to explore the codebase in parallel before synthesising a single, grounded answer. → [Question Answering](features-wiki-qa.md)
- **Copy badge.** On any wiki project's landing page, the **Copy badge** button generates a markdown snippet you can paste straight into your repository's README. It links back to the project's wiki page.

<div class="swiper ms-shots">
<div class="swiper-wrapper">
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-02-overview.jpg" alt="A MewboWiki overview page for Grove showing the Grove Overview article with a runtime-model flow diagram, a left-hand page index, an On this page outline, Refresh and Re-index controls, and an Ask MewboWiki question box" /><figcaption>Prose pages with flow diagrams, grounded in the source files they cite</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-08-qna.jpg" alt="An Ask MewboWiki answer to 'What is this project for?': a summary card, an expandable Cited Sources list, a Retrieval details panel listing the pages accessed and the model used, and a cited answer with inline wiki citation chips and a follow-up question box" /><figcaption>Ask MewboWiki: cited answers with the exact sources that backed them</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-07-badge.jpg" alt="The 'Add the wiki badge' popover in MewboWiki, previewing an 'Ask Mewbo Wiki' badge and the markdown snippet to drop into a repository README, beside the Edit Wiki, Graph, Copy badge, and Copy link toolbar" /><figcaption>Copy a README badge that links straight back to the wiki</figcaption></figure></div>
</div>
<div class="swiper-pagination"></div>
<div class="swiper-button-prev"></div>
<div class="swiper-button-next"></div>
</div>

---

## Explore the docs { .ms-h2-icon data-icon="book" }

The Agentic Wiki has three moving parts. Start wherever matches what you want to do.

<div class="ms-grid ms-grid--3">

<a class="ms-card" href="features-wiki-indexing/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:database" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Indexing &amp; Generation</span>
  <span class="ms-card__body">Point at a repo and a seven-phase agent run clones it, lifts its code into a graph, and writes the pages. Scope the index with a short wizard, then re-index on demand as the code changes.</span>
</a>

<a class="ms-card" href="features-wiki-qa/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:message-circle-question" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Question Answering</span>
  <span class="ms-card__body">Ask MewboWiki a question and a coordinating agent fans out probes across the graph, then synthesises one cited answer. The same Q&amp;A is exposed to your agent fleet over MCP.</span>
</a>

<a class="ms-card" href="features-wiki-graph/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:waypoints" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">The Knowledge Graph</span>
  <span class="ms-card__body">A three-layer multiplex code memory graph (structure, abstract entities, and anchored notes over one shared graph) is what makes answers fast, multi-hop, and authoritative.</span>
</a>

</div>

---

## Enabling it

The wiki ships as an opt-in extra on the API server:

```bash
# Install with the wiki extras
uv sync --extra wiki

# Run the API server
uv run mewbo-api
```

The `/v1/wiki/*` routes mount only when the extras resolve; without them the server starts cleanly with the feature absent. Persisting wikis and live indexing progress use the MongoDB storage backend.

> [!NOTE] Private-repo tokens are remembered
> Access tokens entered during the indexing wizard are persisted, so re-index runs don't prompt you again. If a token is revoked or expires, the next re-index detects the failure and prompts for a replacement.

> [!NOTE] Going deeper
> The wiki runs on the same engine as everything else. See [Sub-agents](features-agents.md) for the delegation model that writes pages in parallel, and [Architecture Overview](core-orchestration.md) for the session runtime underneath.
