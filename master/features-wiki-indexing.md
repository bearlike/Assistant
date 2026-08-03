# Indexing &amp; Generation

Generating a wiki is a normal Mewbo session, not a separate service: an agent owns the run, persists state as it goes, and streams progress back to the console live. Point it at a repository and the run does the rest.

> [!IMPORTANT] Install the wiki extra first
> Before you can index anything, the wiki extra has to be installed and the API server running. See [Enabling it](features-wiki.md#enabling-it) on the Agentic Wiki overview.

<div class="swiper ms-shots">
<div class="swiper-wrapper">
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-04-index-source.jpg" alt="Step one of the Configure indexing wizard, Source: choosing a Git repository, entering a repository URL, and selecting the Gitea platform with an optional access-token field" /><figcaption>Step 1, Source: pick the repository and auto-detect its host</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-05-index-generation.jpg" alt="Step two of the Configure indexing wizard, Generation: picking the Comprehensive wiki depth, the English language, and the claude-sonnet-4-6 authoring model" /><figcaption>Step 2, Generation: depth, language, and authoring model</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-06-index-scope.jpg" alt="Step three of the Configure indexing wizard, Scope: an Exclude-paths filter listing node_modules, dist, and .git plus lockfile and map-file patterns to skip" /><figcaption>Step 3, Scope: exclude noise, or index only a focused subset</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="../assets/img/mewbo-wiki-09-indexing-progress.png" alt="A live indexing progress card showing the clone, scan, graph, enrich, plan, pages, and finalize stages" /><figcaption>The run: live progress through clone, scan, graph, enrich, and finalize</figcaption></figure></div>
</div>
<div class="swiper-pagination"></div>
<div class="swiper-button-prev"></div>
<div class="swiper-button-next"></div>
</div>

---

## The indexing pipeline

A run is a fixed seven-phase pipeline:

```
clone → scan → graph → enrich → plan → pages → finalize
```

1. **Clone** the repository (private repos accept an access token in the wizard).
2. **Scan** every source file.
3. **Graph** the code into a property graph: parse each file's AST with tree-sitter, then lift files, classes, functions, methods, and interfaces into nodes linked by their call, import, and definition relationships.
4. **Enrich:** extract the abstract entity layer over that structure. → [The Knowledge Graph](features-wiki-graph.md)
5. **Plan** the set of pages to write.
6. **Pages:** sub-agents write each page in parallel, grounded in the scanned source. → [Sub-agents](features-agents.md)
7. **Finalize:** dedupe, attach the repository description, and publish.

The landing-page card and the indexing screen read the same progress signal, so they never disagree about which phase a run is in.

As it writes, the indexer also **deposits a few durable notes** into the memory layer: short, anchored facts about each subsystem. The wiki starts out already knowing the non-obvious things about the codebase. → [The memory grows as the wiki is used](features-wiki-graph.md#the-memory-grows-as-the-wiki-is-used)

---

## Supported languages

The graph phase parses source with tree-sitter, so the structural code graph is built only for languages Mewbo ships a grammar for. Today that set is:

| Language | File extensions |
|---|---|
| Python | `.py` |
| JavaScript | `.js`, `.jsx` |
| TypeScript | `.ts`, `.tsx` |
| Go | `.go` |
| Rust | `.rs` |
| Kotlin | `.kt` |
| Java | `.java` |

A file in any other language is still scanned, and page-writing sub-agents can still read it, but it contributes no nodes to the code graph. Markdown is the common case. A `README`, a `CLAUDE.md`, or a docs page is read as source prose and seeds the entity layer (the concepts it names can become entities), yet it produces no code-graph nodes of its own.

---

## Tailor the index first

Before the run starts, a short wizard scopes it to your repository in three steps:

- **Source**: point at a Git repository or a document catalogue. The host is auto-detected (GitHub, GitLab, Gitea, Bitbucket, Azure DevOps, or any generic Git URL), and a private repo can take an access token that is held only for the session.
- **Generation**: choose the depth (**Comprehensive** for full coverage, or **Concise** for a fast tour), the wiki language, and the model that authors every page.
- **Scope**: trim what gets indexed. Lockfiles, build output, and vendored code are excluded by default; switch to *Include only* to index a focused subset.

> [!TIP] Resilient to model changes
> If the model assigned to an indexing run becomes unavailable mid-run (retired, quota-exceeded, or otherwise unreachable), Mewbo automatically switches to the next model on its fallback ladder and continues from the last checkpoint. A transparent event is logged in the console so you can see that a switch happened and why. Long indexing runs complete even when individual models go dark.

---

## Re-index on demand

When a repository changes, **Re-index this wiki** refreshes it **on demand**. It compares what actually changed since the last run and recomputes only the affected scope: the pages, notes, and graph nodes the edit touched. It does not rebuild everything. A small change stays a small, fast update; a stale note is retired, not silently kept.

> [!NOTE] Ground the output with repo notes
> If the repository contains a `.mewbo/wiki.json` file, the indexer adopts its page plan and folds its notes into the page-writing prompts. It's the simplest way to steer which pages get written and to inject facts the code alone won't reveal.
