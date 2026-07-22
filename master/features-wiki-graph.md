# The Knowledge Graph

Every indexed repository ships with a **Graph** view: a live, interactive 3D map of the codebase, built from the same index that backs the pages. Orbit, zoom, and fly through the structure to explore it. Nodes are coloured by symbol type (file, class, function, method, interface) and the legend counts each. The directory tree drives a folder-collapse level of detail: collapse a folder into a single node to keep even a large repository readable, then expand it to drill back in. Use the view to find the dense centres of a project, trace how a subsystem connects to the rest, or jump from a symbol to the page that documents it. Agentic Search reuses the very same 3D view to render a workspace's capability graph, so the wiki and search share one graph explorer.

This graph is more than a picture. It is the substrate the whole wiki stands on. It carries the wiki from single-file lookups to repository scale, and it is what makes [Ask MewboWiki](features-wiki-qa.md) fast and authoritative.

---

## Three layers, one graph

The graph is a **three-layer multiplex**. Use the layer toggle in the console to show one layer, two, or all three at once:

- **Structural (AST) layer:** files, classes, functions, methods, and interfaces as nodes; call, import, and definition relationships as edges.
- **Entity layer:** abstract entities extracted after the AST phase: the people, subsystems, components, and concepts that matter in the codebase. Each entity has a deterministic id and is grounded in real AST symbols and source prose, never in generated text. Cross-layer *anchor edges* connect entity nodes to the AST symbols they describe, so you can see exactly how a concept maps to code.
- **Memory layer:** atomic notes anchored to both structural symbols and entity nodes.

---

## Grounded by a code memory graph

Most documentation tools chop a repository into text and search it like prose. Mewbo builds a graph instead. Indexing lifts each file's AST into a **code property graph** (the structural layer), then adds an **entity layer** of abstract concepts extracted via a GraphRAG-grounded enrichment pass, and finally a **memory layer** of atomic notes anchored to symbols and entities. Structure, concepts, and meaning live as separate dimensions over one shared graph: a **multiplex code memory graph** that remembers both how the code is wired and what it means.

That graph is what makes answers fast and authoritative. A question doesn't pull a handful of look-alike snippets; it traverses the graph across **multiple hops** (a symbol, to its callers, to the module that owns them, to the note that explains why), assembling a connected, repository-scale context before the model writes a word. Keyword and semantic search seed the entry points; the memory graph expands them into the full picture. Because every note is anchored, a stale answer can't hide: when the code under a note changes, the next on-demand refresh re-checks the note and retires it if it no longer holds.

> [!NOTE] Why multi-hop matters
> A flat search answers a cross-module question with three disconnected results. Traversing the memory graph follows real relationships in the code instead, so the answer holds together and every hop stays grounded. Semantic embeddings are an optional accelerator. Without them, keyword retrieval still seeds the graph and the wiki keeps working.

---

## The memory grows as the wiki is used

The memory layer is not frozen at indexing time. It compounds:

- **The indexer seeds it** while writing pages, depositing short anchored facts about each subsystem.
- **Ask MewboWiki feeds it.** When a Q&A run finds a durable fact worth remembering, Mewbo automatically deposits a note: validated, condensed, anchored to the right symbols, and merged like any other insight, off the critical path so it doesn't slow the answer down. → [Question Answering](features-wiki-qa.md)
- **You and your agents can teach it explicitly.** Submit a one-line fact the code alone won't reveal, and Mewbo condenses, de-duplicates, and merges it. Every future answer is built on the accumulated set.

> [!TIP] Contribute an insight
> Agents on your fleet can teach the wiki as they work via the [MCP server](clients-mcp.md)'s `submit_insight` tool or the REST endpoint. Each insight is validated, condensed to an atomic note, anchored to the code it's about, and safely merged, so the knowledge compounds instead of drifting into duplicates.
