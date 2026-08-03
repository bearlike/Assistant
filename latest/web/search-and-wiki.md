# Search and Wiki

The console hosts two products built on Mewbo's engine. Agentic Search runs a question across many sources and returns a cited answer. The Wiki turns a repository into browsable pages, a question-and-answer surface, and an interactive 3D code graph. This page is a product walkthrough. It shows what each surface does and how to drive it. The deeper capability docs are linked throughout.

## Agentic Search

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-search-01-landing.jpg" alt="The Agentic Search landing page with the search composer and saved workspaces" style="width: 100%; max-width: 880px; height: auto;" />
</div>

Agentic Search answers a question by fanning out across the sources in a workspace. A workspace is a saved set of sources, such as code hosts, documentation providers, and web search. You ask a question, the agent probes each source, and it returns a synthesized answer with its supporting results. The whole product lives at the `/search` route, driven by [`AgenticSearchView`](repo:apps/mewbo_console/src/components/agentic_search/AgenticSearchView.tsx).

### Run a search

Type a question into the search composer and submit. The landing page is inert until you do. Opening it never starts a run on its own.

The composer toolbar carries exactly two controls. One names the active workspace. The other is the scope control, [`SearchScopeControl`](repo:apps/mewbo_console/src/components/agentic_search/SearchScopeControl.tsx), which hosts the tier, the model, and the source configuration behind a single menu. The scope pill names its resolved setting at rest, so you can read the current tier and model without opening anything.

### Tiers

The tier is one budget knob. It sets how deep the agent decomposes the question and how wide it fans out its probes. It also presets the model that drives the run.

| Tier | Budget |
|------|--------|
| Fast | Shallow decomposition, few probes |
| Auto | Balanced, the default |
| Deep | Maximum depth, wide fan-out |

Pick a tier from the scope menu. Picking a tier clears any model override, so a stale model from a previous tier never silently wins. You can still override the model for a single run from the same menu, without editing your config.

### The results and cited answer view

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-search-02-results.jpg" alt="The Agentic Search results view with a synthesized answer, result cards, and the trace rail" style="width: 100%; max-width: 880px; height: auto;" />
</div>

The results view is a centered content column with a trace rail on the right. Everything you see derives from events the run actually emitted, never from a synthetic timer.

**The answer.** The synthesized answer renders at the top as markdown, streaming in as the agent produces it. Its confidence and source count are shown only when the run earned them. An unknown value is left blank, never faked as zero.

**Result cards.** Each supporting result is a card with its source, rank, relevance, title, snippet, and a footer of structured metadata. The title links out to the source when a URL exists. A card is a reading surface. It expands to more detail only when there is more to show. A per-card follow-up button prefills the composer with a question about that result.

**The instrument rail.** The right rail reads per-lane and run-level instruments. Each lane is one probe, leading with its kind, a prominent result-count pip, and a strip of its model, step count, duration, and token usage. Every field renders only when it is present. Nothing is fabricated. Below 1100 pixels the rail hides and the trace stays reachable from a button in the run meta row.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-search-04-agent-trace.jpg" alt="The Agentic Search agent trace drawer showing per-lane instrument detail" style="width: 100%; max-width: 880px; height: auto;" />
</div>

**The agent trace.** Open the trace for the full per-agent detail. It shows each lane's kind, model, steps, and its terminal evidence. From the run meta row you can also open the underlying agent session, cancel a running search, or go one tier deeper.

A finished run is addressable. A `/search?run=<id>` URL replays the durable snapshot with no re-execution, so a search is shareable across browsers.

For the search engine itself, the tier model, and the source-capability graph that backs it, see [Agentic Search](../features-search.md) and [Source Capability Graph](../features-search-scg.md).

## Wiki

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-01-landing.jpg" alt="The MewboWiki landing page showing a gallery of indexed repositories" style="width: 100%; max-width: 880px; height: auto;" />
</div>

The Wiki turns a repository into an auto-generated documentation site. It indexes the code, writes pages, builds a knowledge graph, and answers questions grounded in the source. The Wiki lives at `/wiki`, resolved by [`WikiApp`](repo:apps/mewbo_console/src/components/wiki/WikiApp.tsx). To index a repository, browse the wiki gallery and follow the configure wizard. See [Wiki indexing](../features-wiki-indexing.md) for that flow.

Indexing is a separate act from Mewbo knowing about a repository at all. A repository is a product-level record, registered once and used by whichever surfaces you point at it; a wiki project is an index of one, and only exists because someone asked for it. Registering costs nothing and starts nothing. Generating the wiki is the deliberate step, and this is where you take it.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-04-index-source.jpg" alt="The configure-indexing wizard's first step, choosing a Git platform and repository URL to index" style="width: 100%; max-width: 880px; height: auto;" />
</div>

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-09-indexing-progress.png" alt="A live indexing progress card showing the clone, scan, graph, enrich, plan, pages, and finalize stages" style="width: 100%; max-width: 720px; height: auto;" />
</div>

### Browse the pages

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-02-overview.jpg" alt="A MewboWiki page with the navigation sidebar, article content, and table of contents" style="width: 100%; max-width: 880px; height: auto;" />
</div>

A wiki page is a three-column layout. A sidebar navigates between pages. The article renders in the middle as markdown with syntax highlighting and diagrams. A table of contents tracks your position on the right. Every page a maintainer can copy a badge for, linking straight back into the wiki.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-07-badge.jpg" alt="The Add the wiki badge dialog, with a markdown snippet for embedding an Ask Mewbo Wiki badge in a README" style="width: 100%; max-width: 640px; height: auto;" />
</div>

### Ask a question with citations

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-wiki-08-qna.jpg" alt="The MewboWiki question-and-answer view with an answer, inline citation chips, and source cards" style="width: 100%; max-width: 880px; height: auto;" />
</div>

The question-and-answer surface, [`QAScreen`](repo:apps/mewbo_console/src/components/wiki/QAScreen.tsx), answers a question against the indexed repository. Ask from the floating composer. The answer streams into the left column. Citations render inline as chips inside the prose.

Click a citation chip and it navigates to the cited source. When the source is a file with a known repository, the chip opens it at the exact line range in a new tab. Otherwise it scrolls to a source card on the right. Each source card shows the cited evidence in place. A file card shows the line-numbered excerpt. A page card shows the body of the page the answer grounded on. A completed answer is addressable through a `?answer=<id>` URL, so a refresh or a shared link replays it with no new model call.

For the retrieval and citation model behind the Wiki, see [Wiki](../features-wiki.md) and [Wiki question and answer](../features-wiki-qa.md).

### The 3D code graph

The Wiki includes an interactive 3D graph of the repository. It is a WebGL Code Galaxy, not a flat diagram. It renders through the shared engine [`Graph3DView`](repo:apps/mewbo_console/src/components/wiki/Graph3DView.tsx), used by both the wiki knowledge graph, [`KnowledgeGraph3DScreen`](repo:apps/mewbo_console/src/components/wiki/KnowledgeGraph3DScreen.tsx), and the Agentic Search workspace graph.

The graph is a three-layer multiplex. The code layer holds the parsed structure of files, classes, functions, and methods. An entity layer holds abstract concepts. A memory layer holds notes anchored to the code. Directory hierarchy drives the level of detail. Folders start collapsed and expand on demand, so even a large repository stays responsive rather than choking on tens of thousands of nodes at once. The toolbar filters nodes by name, toggles kinds and layers, and fits or resets the view. Click a node to inspect it in a side panel.

The same engine powers the Agentic Search workspace graph, where the nodes are a workspace's mapped source capabilities and their anchored memory notes.

For the graph substrate and its schema, see [Wiki code graph](../features-wiki-graph.md).

## Next steps

- [Sessions](sessions.md): the session list, the composer, and live streaming.
- [Agentic Search](../features-search.md): the search engine, tiers, and multi-source retrieval.
- [Source Capability Graph](../features-search-scg.md): the graph that backs search over saved sources.
- [Wiki](../features-wiki.md): indexing, page generation, and grounded answers.
- [Wiki code graph](../features-wiki-graph.md): the multiplex graph substrate.
