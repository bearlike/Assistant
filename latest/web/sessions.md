# Sessions

A session is one conversation with the agent. The console is built around watching sessions run. This page covers the session list, the composer that starts a session, what you see while a run is live, and how to resume a session that stopped.

## The session list and landing page

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-console-01-front.png" alt="The Mewbo console landing page with the composer and a list of recent sessions" style="width: 100%; max-width: 880px; height: auto;" />
</div>

The console home is both the composer and the full task list. Recent sessions show newest first, each row carrying its title, timestamp, and project.

Not every session is yours to read. The console spawns sessions internally for wiki indexing, for search runs, for realtime answers, and for Mewbo Apps builder and maintainer work. Those would bury your own tasks. So the landing page classifies each session by where it came from and hides the internal kinds by default. The origin is computed on the server, not guessed by the browser, and every row wears an origin chip beside its timestamp through [`SessionOriginBadge`](repo:apps/mewbo_console/src/components/SessionOriginBadge.tsx).

By default you see sessions you started and sessions from chat channels. The origin filter, a dropdown on the list, lets you reveal the rest. Wiki, search, realtime, Mewbo Apps, and mobile sessions sit behind it. App sessions are the ones that build and maintain a Mewbo App, so they belong to the Apps product rather than to your task list. Mobile sessions belong to the Aura Android client and scope themselves to its own rail. The filter and the badge live in [`HomeView`](repo:apps/mewbo_console/src/components/HomeView.tsx).

On a session detail page, a persistent left panel, [`TaskSidebar`](repo:apps/mewbo_console/src/components/TaskSidebar.tsx), lists your recent tasks so you can switch between them without leaving the run. It honors the same default origin filter as the landing page. On a phone it collapses into a drawer.

## Start a session with the composer

The composer is where a session begins. It looks simple by default. Every option below it is optional, and the defaults are enough for a first run. The controls, all wired through [`InputBar`](repo:apps/mewbo_console/src/components/InputBar.tsx), let you scope the run before you submit.

**Project scope.** Pick which project the session runs in. Each project has its own working directory, its own MCP server config, and its own skills. A session can also anchor to a branch of a project, or run inside an isolated git worktree so parallel sessions never collide. See [Branches and Worktrees](../features-worktrees.md).

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-console-06-projects.png" alt="The Projects page listing virtual workspaces, each with its description and working directory" style="width: 100%; max-width: 880px; height: auto;" />
</div>

**Model.** Choose the model for the session. You can also set a per-message model that applies to a single turn without changing the session default. A fallback tab lets you build an ordered ladder of models. When the primary model keeps failing, the run escalates down the ladder in order.

**Tool scope.** Pick which MCP tools the agent can use for this session. The composer lists the tools available on the selected project, and you toggle the ones you want in scope. Narrowing the tool set keeps the agent focused and its context lean.

**Skills and references.** Type `/` to browse skills and slash commands as you type. Type `@` to reference a file, a directory, a git diff, or a URL. The reference expands into a bounded context block at submit time, before the model runs. You can also attach files from the composer to inject their content into the session.

Submit, and the console creates the session through the API and opens its detail view.

## Watch a run live

Once a run starts, the detail view becomes an instrument panel. The left side is the conversation. The right side is the workspace, where tool output renders.

**Streaming responses.** The assistant reply grows token by token as the model produces it. The console renders each delta as it arrives, so you read the answer forming rather than waiting for the whole turn.

**The progress card.** When the agent lays out a plan, a live checklist tracks it. Each item shows its status and updates in place as the agent works through the plan. The card is driven by [`TodoCard`](repo:apps/mewbo_console/src/components/TodoCard.tsx) from the authoritative progress events the agent emits, not from parsed text.

**Live throughput.** A run readout shows the elapsed time, the live tokens-per-second rate, and the current phase. It appears only while a run is active. The same [`RunTelemetry`](repo:apps/mewbo_console/src/components/RunTelemetry.tsx) data renders compact in the composer strip and full in the workspace spinner, so status lives in one place, never two competing readouts.

**Tool output.** Shell commands, file edits, file reads, permission prompts, and completions render as cards in the workspace panel. Plan approvals and file diffs get their own rich cards.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-console-03-plan-approval.jpg" alt="A plan approval card in the Mewbo console workspace panel" style="width: 100%; max-width: 720px; height: auto;" />
</div>

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-console-04-file-edit.jpg" alt="A file diff card showing additions and deletions to a CLAUDE.md file" style="width: 100%; max-width: 720px; height: auto;" />
</div>

## Follow sub-agent activity

Mewbo runs sub-agents for parallel or specialized work. The console surfaces them so you can see the whole tree, not just the root turn. As sub-agents start and stop, an activity indicator in the conversation tracks the active agents with their task descriptions. Sub-agent logs show lifecycle status and step count. Each sub-agent's result is parsed and rendered as a structured card when it completes.

## Steer, interrupt, and share

You do not have to wait for a run to finish. The composer stays alive during a run.

**Queue a message.** While the run is active, send a message and it is queued for the agent. The composer shifts into a steering mode, its Send button becoming Queue.

**Stop the run.** A Stop control appears in the composer. Stopping is a destructive action, so it always asks for a two-step confirmation before it cancels.

**Fork from a message.** Hover any message and choose to fork from that point. The console creates a new session with history up to that message, so you can explore a different path without losing the original.

**Share and export.** Share a session to get a read-only link that works without authentication. Export a session to download its full payload.

The exact request shapes for steering, forking, sharing, and export live in the [REST API Reference](../rest-api.md).

## Resume and recover a session

A session can stop before it finishes. The model may fail repeatedly, or the run may be interrupted. Those sessions are recoverable, not lost.

When a session is recoverable, the console offers two choices on the session, both wired through the shared recovery hook in [`SessionDetailView`](repo:apps/mewbo_console/src/components/SessionDetailView.tsx). **Continue** resumes the session from where it stopped. **Retry** restarts the last turn. The last recoverable failure also surfaces inline in the conversation, so the reason is visible next to the recovery controls rather than hidden away.

Recovery is generic across the console. A stopped [wiki indexing](../features-wiki-indexing.md) session routes back into its own indexing screen when you resume it.

## Next steps

- [Search and Wiki](search-and-wiki.md): the Agentic Search product and the Wiki, including the 3D code graph.
- [Web IDE](ide.md): open a full browser IDE tied to a session.
- [Widgets](widgets.md): interactive panels rendered inline in a session.
- [Branches and Worktrees](../features-worktrees.md): anchor a session to a branch or an isolated worktree.
- [REST API Reference](../rest-api.md): the session endpoints the console calls.
