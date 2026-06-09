<!--
  Maintainer note: this landing page is a decision surface, not a sitemap.
  The sidebar already enumerates pages. Every section here should help a
  visitor self-select a path (surface, journey stage, capability cluster).
  Keep positioning aligned with README.md when core messaging changes.
-->

<section class="ms-hero" markdown>

<p class="ms-hero__eyebrow">Documentation</p>

### An open stack for agentic work, grounded in your own knowledge.

<p class="ms-hero__lede">
Real work outgrows a single context, a single tool, a single attempt. Mewbo's hypervisor
splits a goal into parallel agents you watch as a live tree and steer mid-run. Three products
stand on that one foundation. Automation that does the multi-step work and isolates every
change. A wiki that turns your codebase into a graph you can question. Search that reaches
across every tool you connect and returns one ranked list. Every layer runs on any model.
All of it is open source.
</p>

<div class="ms-cta-row">
  <a class="ms-btn ms-btn--primary" href="getting-started/">Quickstart →</a>
  <a class="ms-btn ms-btn--secondary" href="deployment-docker/">Install via Docker</a>
  <a class="ms-btn ms-btn--ghost" href="reference/">API reference</a>
</div>

<div class="ms-pills" aria-label="What makes Mewbo different">
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:git-fork" width="14" height="14" aria-hidden="true"></iconify-icon>
    Parallel sub-agents
  </span>
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:monitor-play" width="14" height="14" aria-hidden="true"></iconify-icon>
    Per-session Web IDE
  </span>
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:sparkles" width="14" height="14" aria-hidden="true"></iconify-icon>
    Live LSP diagnostics
  </span>
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:check-check" width="14" height="14" aria-hidden="true"></iconify-icon>
    Claude Code and Codex compatible
  </span>
  <span class="ms-pill">
    <iconify-icon class="ms-pill__icon" icon="lucide:layout-panel-top" width="14" height="14" aria-hidden="true"></iconify-icon>
    Interactive widgets inline in chat
  </span>
</div>

<div class="swiper ms-shots">
<div class="swiper-wrapper">
<div class="swiper-slide"><figure><img loading="lazy" src="mewbo-console-01-front.png" alt="The Mewbo Console home listing recent sessions" /><figcaption>Your sessions at a glance</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="mewbo-console-02-tasks.png" alt="A Mewbo task in the console, broken into steps with tool calls and results" /><figcaption>Inside a task, step by step</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="mewbo-console-07-widgets.png" alt="Interactive widgets rendered inline in a Mewbo conversation" /><figcaption>Interactive widgets, inline in chat</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="mewbo-wiki-02-overview.jpg" alt="A MewboWiki overview page with a runtime flow diagram and an Ask MewboWiki box" /><figcaption>Agentic Wiki: documentation grounded in your code</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="mewbo-wiki-03-graph.jpg" alt="The MewboWiki interactive knowledge graph of a repository" /><figcaption>Your codebase as a living knowledge graph</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="mewbo-search-01-landing.jpg" alt="The Agentic Search landing page with workspaces scoped to connected sources" /><figcaption>Agentic Search: workspaces over your connected tools</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="mewbo-search-02-results.jpg" alt="Agentic Search results: one ranked list across connected sources with a synthesised overview" /><figcaption>One ranked list across every tool, topped by a synthesis</figcaption></figure></div>
<div class="swiper-slide"><figure><img loading="lazy" src="mewbo-console-05-plugins.png" alt="The Mewbo plugins page with installed plugins and marketplace listings" /><figcaption>Plugins and a marketplace to extend any session</figcaption></figure></div>
</div>
<div class="swiper-pagination"></div>
<div class="swiper-button-prev"></div>
<div class="swiper-button-next"></div>
</div>

</section>

---

## Choose your surface { .ms-h2-icon data-icon="target" }

One engine. Five clients. Pick whichever matches where the work already happens inside your team. Behaviour, tools, and configs are identical across all of them; the mode of access is what changes.

<div class="ms-grid ms-grid--5">

<a class="ms-card" href="clients-cli/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:terminal" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">CLI</span>
  <span class="ms-card__body">For developers shipping code. Run tests, refactors, and migrations alongside your git workflow, with plan-mode approval ahead of any destructive step.</span>
</a>

<a class="ms-card" href="clients-web-api/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:app-window" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Web console &amp; API</span>
  <span class="ms-card__body">For teams that need audit trails. Review sessions side by side, and drive scheduled runs from CI, cron, or your internal ops stack.</span>
</a>


<a class="ms-card" href="clients-nextcloud-talk/">
  <span class="ms-card__icon ms-card__icon--multi">
    <iconify-icon icon="simple-icons:slack" width="16" height="16" aria-label="Slack"></iconify-icon>
    <iconify-icon icon="simple-icons:microsoftteams" width="16" height="16" aria-label="Microsoft Teams"></iconify-icon>
    <iconify-icon icon="simple-icons:nextcloud" width="16" height="16" aria-label="Nextcloud Talk"></iconify-icon>
  </span>
  <span class="ms-card__title">Chat platforms</span>
  <span class="ms-card__body">For the channels where your team already talks. Native safe adapters for Slack, Microsoft Teams, and Nextcloud Talk.</span>
</a>


<a class="ms-card" href="clients-email/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:mail" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Email</span>
  <span class="ms-card__body">For colleagues who work from the inbox. Execs, external clients, and field staff send a normal email and get a styled reply minutes later.</span>
</a>

<a class="ms-card" href="clients-home-assistant/">
  <span class="ms-card__icon">
    <iconify-icon icon="simple-icons:homeassistant" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Home Assistant</span>
  <span class="ms-card__body">For facilities running HA OS, operators can drive meeting room setup, occupancy checks, and deployment checks across every exposed sensor by text or voice.</span>
</a>

</div>

---

## How it works { .ms-h2-icon data-icon="flow" }

Every product on this page runs the same loop underneath. You ask. Mewbo splits the work. You get one answer you can trace back to its sources.

### You describe an outcome

Ask for it in plain English on whichever surface is closest. In plan mode, the root agent drafts the steps first and waits for your approval. Destructive work never runs before you sign off on the plan.

### Mewbo delegates in parallel

The root agent spawns sub-agents for every piece of work that can run at once. A test run, a search, a refactor, and an MCP call against an external service all execute in parallel. A live hypervisor sits over the run like a control tower. It watches every child for stalls, nudges drifting agents back on course with plain-language corrections between tool steps, and holds each one to its token budget without killing work already in flight. The tree grows in real time, and you can steer or cancel any branch.

### You get a synthesised answer, not a pile of logs

Each sub-agent returns a structured result: status, summary, warnings, files touched, and acceptance-criteria checks. The root synthesises them into one coherent answer, alongside a full transcript of every tool call, every permission prompt, and every compaction. Fork any message to branch the conversation, replay a failure against a different model, or hand the whole thing off to a teammate.

!!! note "Go deeper"

    Want the internals? See [Architecture Overview](core-orchestration.md) for the tool-use loop, hypervisor, and structured-concurrency lifecycle.

---

## What's new { .ms-h2-icon data-icon="star" }

<div class="ms-grid ms-grid--2">

<a class="ms-card" href="features-wiki/">
<span class="ms-card__title">Agentic Wiki</span>
<span class="ms-card__body">Stop reading the codebase. Ask it. Mewbo lifts your code's ASTs into a multiplex memory graph: structure on one layer, LLM-attached meaning on another. Sub-agents write grounded pages in parallel, every claim traced back to source. A question then travels the graph across many hops, so answers cite where they came from instead of approximating from the open web. Or browse the whole repository as a live, zoomable graph.</span>
</a>

<a class="ms-card" href="features-search/">
<span class="ms-card__title">Agentic Search</span>
<span class="ms-card__body">Your team's knowledge hides in repos, trackers, chat, and docs. One question fans a sub-agent out to each connected source in parallel. Their hits merge and re-rank into a single list that spans every source, topped by a synthesised overview cited to its origins, with a trace of every source it queried. One question. Every tool. One ranked answer.</span>
</a>

<a class="ms-card" href="features-widgets/">
<span class="ms-card__title">Widgets inline in chat</span>
<span class="ms-card__body">Ask for a chart, a card, or a data table and an interactive widget appears directly in the conversation. Widgets run in a sandboxed browser environment with no server involvement. Data is baked in at creation time, so widgets persist across sessions as permanent snapshots. Teams with internal data systems that lack good reporting interfaces can surface results visually on demand.</span>
</a>

<a class="ms-card" href="features-plugins/">
<span class="ms-card__title">Plugin and Agent Skills platform</span>
<span class="ms-card__body">Extend Mewbo with new agent types, skills, hooks, and tools using the same plugin format as Claude Code. Plugins are compatible with the official Claude plugins marketplace and activate automatically at session start. Capability gating ensures features only appear on surfaces that can support them. The bundled widget-builder is the reference example.</span>
</a>

<a class="ms-card" href="clients-mcp/">
<span class="ms-card__title">Mewbo as an MCP server</span>
<span class="ms-card__body">Expose Mewbo to your whole agent fleet. Claude Code, Codex, Cursor, or another Mewbo connects over MCP to start coding sessions on a fresh worktree, steer and read them back at the detail it needs, and ask grounded questions of your Agentic Wiki — authenticated with a key you issue and revoke.</span>
</a>

</div>

---

## Already using Claude Code or Codex? { .ms-h2-icon data-icon="plug" }

Mewbo reads the configuration you already have. Point it at a project and it picks up your MCP servers, skills, plugins, and instruction hierarchy automatically. No rewrites, no new formats.

<div class="ms-grid ms-grid--5">

<div class="ms-card">
<span class="ms-card__title">MCP servers</span>
<span class="ms-card__body">Both the Mewbo <code>servers</code> and the Claude Code / VS Code <code>mcpServers</code> schemas are accepted at project and user scope.</span>
</div>

<div class="ms-card">
<span class="ms-card__title">Skills</span>
<span class="ms-card__body"><code>SKILL.md</code> files in <code>~/.claude/skills/</code> or <code>.claude/skills/</code> activate exactly as authored, with the Agent Skills standard.</span>
</div>

<div class="ms-card">
<span class="ms-card__title">Plugins &amp; marketplaces</span>
<span class="ms-card__body">Claude Code plugin manifests install without translation. Point Mewbo at any Claude Code-compatible marketplace and it just works.</span>
</div>

<div class="ms-card">
<span class="ms-card__title">Project instructions</span>
<span class="ms-card__body"><code>CLAUDE.md</code>, <code>AGENTS.md</code>, and <code>.claude/rules/*.md</code> all load hierarchically on session start.</span>
</div>

<a class="ms-card" href="features-plugins/#session-tools">
<span class="ms-card__title">Session tools</span>
<span class="ms-card__body">Plugins contribute per-agent stateful tools via a <code>session_tools</code> array in <code>plugin.json</code>. The core imports the class and wires it to the <code>ToolUseLoop</code>; widgets, exit-plan-mode, and future capability bundles all use the same primitive.</span>
</a>

</div>

!!! note "See also"

    [Project Setup](project-configuration.md) and [Plugins &amp; Marketplace](features-plugins.md) walk through the complete compatibility matrix.

---

## What you can do { .ms-h2-icon data-icon="grid" }

<div class="ms-grid ms-grid--5">

<div class="ms-card">
<span class="ms-card__title">Workspace &amp; execution</span>
<ul class="ms-card__list">
  <li><a href="features-builtin-tools/">Built-in tools</a>: read, edit, shell, list</li>
  <li><a href="features-web-ide/">Web IDE</a>: per-session code-server</li>
  <li><a href="features-lsp/">Code intelligence (LSP)</a></li>
  <li><a href="features-mcp/">External tools (MCP)</a></li>
  <li><a href="features-widgets/">Widgets</a>: interactive UI inline in chat</li>
</ul>
</div>

<div class="ms-card">
<span class="ms-card__title">Knowledge &amp; discovery</span>
<ul class="ms-card__list">
  <li><a href="features-wiki/">Agentic Wiki</a>: source-grounded repo docs</li>
  <li><a href="features-wiki/#the-knowledge-graph">Knowledge graph</a> of the codebase</li>
  <li><a href="features-search/">Agentic Search</a> across connected MCPs</li>
</ul>
</div>

<div class="ms-card">
<span class="ms-card__title">Composition &amp; delegation</span>
<ul class="ms-card__list">
  <li><a href="features-agents/">Sub-agents and the hypervisor</a></li>
  <li><a href="features-skills/">Skills (Agent Skills standard)</a></li>
  <li><a href="features-plugins/">Plugins and marketplace</a></li>
</ul>
</div>

<div class="ms-card">
<span class="ms-card__title">Control &amp; safety</span>
<ul class="ms-card__list">
  <li><a href="features-plan-mode/">Plan mode</a>: review before execution</li>
  <li><a href="features-permissions-hooks/">Permissions and hooks</a></li>
  <li><a href="features-policies/">Policies</a>: semantic gate-checks on tool calls</li>
  <li><a href="features-monitors/">Monitors</a>: session-wide behavioural guardrails</li>
  <li><a href="troubleshooting/">Troubleshooting guide</a></li>
</ul>
</div>

<div class="ms-card">
<span class="ms-card__title">Session &amp; context</span>
<ul class="ms-card__list">
  <li><a href="features-token-usage/">Token usage and budgets</a></li>
  <li><a href="features-compaction/">Compaction (FULL / PARTIAL)</a></li>
  <li><a href="session-runtime/">Session runtime</a></li>
</ul>
</div>

</div>

---

## From install to production { .ms-h2-icon data-icon="route" }

A five-step journey. Each step is short, and each link lands on the page you need.

<div class="ms-lifecycle">

<div class="ms-step">
<p class="ms-step__title">Install</p>
<ul class="ms-step__links">
  <li><a href="getting-started/">Get Started</a></li>
  <li><a href="deployment-docker/">Docker Compose</a></li>
</ul>
</div>

<div class="ms-step">
<p class="ms-step__title">Configure</p>
<ul class="ms-step__links">
  <li><a href="llm-setup/">LLM setup</a></li>
  <li><a href="configuration/">Configuration reference</a></li>
  <li><a href="project-configuration/">Project setup</a></li>
</ul>
</div>

<div class="ms-step">
<p class="ms-step__title">Use</p>
<ul class="ms-step__links">
  <li><a href="clients-cli/">CLI</a></li>
  <li><a href="clients-web-api/">Web console and API</a></li>
  <li><a href="clients-mcp/">MCP server</a></li>
  <li><a href="features-plan-mode/">Plan mode</a></li>
</ul>
</div>

<div class="ms-step">
<p class="ms-step__title">Deploy</p>
<ul class="ms-step__links">
  <li><a href="deployment-production/">Production setup</a></li>
  <li><a href="deployment-storage/">Storage backends</a></li>
  <li><a href="features-permissions-hooks/">Permissions and hooks</a></li>
</ul>
</div>

<div class="ms-step">
<p class="ms-step__title">Extend</p>
<ul class="ms-step__links">
  <li><a href="features-mcp/">MCP tools</a></li>
  <li><a href="features-plugins/">Plugins</a></li>
  <li><a href="features-widgets/">Interactive widgets</a></li>
  <li><a href="developer-guide/">Build a client</a></li>
</ul>
</div>

</div>

---

## Keep learning { .ms-h2-icon data-icon="book" }

<div class="ms-grid ms-grid--4">

<a class="ms-card" href="https://github.com/bearlike/Assistant">
  <span class="ms-card__icon">
    <iconify-icon icon="simple-icons:github" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">GitHub repo</span>
  <span class="ms-card__body">Source, issues, and releases.</span>
</a>

<a class="ms-card" href="core-orchestration/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:layers" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Architecture deep-dive</span>
  <span class="ms-card__body">Tool-use loop, hypervisor, and lifecycle.</span>
</a>

<a class="ms-card" href="troubleshooting/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:life-buoy" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Troubleshooting</span>
  <span class="ms-card__body">Common errors and how to diagnose them.</span>
</a>

<a class="ms-card" href="https://github.com/bearlike/Assistant/releases">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:list" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Changelog</span>
  <span class="ms-card__body">Release notes and upgrade guides.</span>
</a>

</div>
