# Agentic Search

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-search-01-landing.jpg" alt="The Agentic Search landing page in the Mewbo Console, with a question box scoped to a workspace, connected-source chips, and a grid of workspaces such as Engineering docs, Support intel, and Research library" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Ask a question in plain English and Mewbo searches across everything you've connected. Sub-agents fan out across a workspace's MCP servers — code hosts, ticket trackers, chat, docs, the web — gather what's relevant, and bring back one ranked, synthesised answer with citations and a trace of where it looked.

---

## Workspaces scope the search

A **workspace** is a named bundle of connected sources for one topic. *Engineering docs* might point at your repos, RFCs, and architecture pages; *Support intel* at customer tickets, Slack threads, and public issues; *Research library* at papers and reading lists. Each workspace shows the MCP sources wired into it, and every question runs against the workspace you pick — so a query about a customer issue doesn't trawl your design system, and vice versa.

Spin up a new workspace whenever you have a new question domain: name it, choose which MCP servers it can reach, and start asking.

---

## One question, parallel sub-agents

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-search-02-results.jpg" alt="An Agentic Search results page showing a synthesised answer with an 86% confidence bar and citations, source-type filters, ranked result cards from GitHub, Slack, and Linear, and a right rail with an agent trace, related questions, and people" style="width: 100%; max-width: 960px; height: auto;" />
</div>

Behind a single question, Mewbo spawns a sub-agent per connected source. They search in parallel, each scoped to just its own service, and their findings are merged and re-ranked into one result set. Because it's the same hypervisor that powers the rest of Mewbo, the fan-out is bounded, observable, and fast — a typical query resolves in seconds.

---

## A synthesised answer, with receipts

The top of every result is a **Synthesis** card: a direct, written answer to your question rather than ten blue links. It carries:

- **Inline citations** — each claim links to the source result it came from.
- **A confidence score** — how strongly the gathered evidence supports the answer.
- **Ask a follow-up** — keep pulling the thread without re-scoping; the workspace and context carry over.

Below it, the underlying **results** are listed in rank order — a merged PR, a Slack thread, a tracker issue — each with its source, status, and a snippet. Filter the list by type with one click: **Docs**, **Code**, **Threads**, **Design**, **Tickets**, or **Web**.

---

## See how it got there

Agentic Search is transparent by design. Alongside each answer:

- **Agent trace** — which sources were queried and which returned a hit, so you can see the search actually ran end to end.
- **Related questions** — the obvious next questions, one click away.
- **People** — who authored, merged, or reported the artefacts behind the answer, pulled straight from the sources.

---

## Availability

Agentic Search lives in the **Mewbo Console**, reachable from the top navigation next to Tasks and Wiki. It reads the MCP servers you've already configured — the same connections used everywhere else in Mewbo — and groups them into workspaces.

> [!NOTE] Going deeper
> Search reuses the same primitives as the rest of the engine — see [External Tools (MCP)](features-mcp.md) for how connected sources are configured, and [Sub-agents](features-agents.md) for the parallel fan-out and the hypervisor that bounds it.
