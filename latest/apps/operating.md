# Living with an App

A live app is a running system. This page covers its heartbeat, the panel that shows whether that heartbeat is healthy, and your levers: feed it, pause it, fix it.

Same running example as the [overview](index.md) and [build guide](building.md): markdown trackers plus a `job-applications.csv`, shown as a dashboard.

---

## The refresh cycle

Each pipeline refreshes the app on the schedule it declared. You never arm anything: the platform arms schedules at submit, and pause or archive takes them down again.

Code pipelines run deterministically: sub-second, zero model cost. A fire reads your files, writes the derived rows into collections, and records a run in the ledger. The open app shows the new data on its next rerun.

```mermaid
flowchart LR
    S([Schedule fires]) -->|"free, sub-second"| R[Read your files]
    R --> W[Write collections] --> L[Ledger + freshness] --> A([App rerenders live])
    R -.->|on breakage| F{{Run fails}} -.-> M[Maintainer repairs] -.-> V([New version])
```

The solid path is the free loop. The dotted branch is the exception: a failed run starts a maintainer repair run, which ships a fixed version, and the schedule carries on.

Agentic pipelines are the declared exception. Each fire is a full model run, so they exist only for refreshes that cannot be expressed as deterministic code.

---

## Reading the health panel

The health rail on the app's detail screen, condensed onto its gallery card. Freshness is computed from the run ledger:

- **Fresh.** The last scheduled run succeeded and the next is due on time.
- **Stale.** The last success is older than the cadence implies. Check recent runs.
- **Never refreshed.** Live, but no pipeline has completed a run. Loud on purpose.
- **No schedule.** A pipeline that would never run on its own. Warned, never silent.
- **On demand.** Deliberately manual. Shown as a choice, not a fault.

Below that: each pipeline's cadence ("refreshes hourly"), on-demand markers, not-armed warnings, and the recent runs with outcome and rows written.

For automation, the whole panel is one call: [GET /api/apps/{app_id}/system](endpoint:GET /api/apps/{app_id}/system). Poll it to alert on staleness from your own tooling.

---

## Interacting with your app

- **Live reads.** The app reads collections on every rerun. New pipeline output appears on the next interaction.
- **Instant filters.** Narrowing to *interview*-stage applications is a local read. No model, no rebuild.
- **Read-through refresh.** A read-through pipeline recomputes only when its source files changed. Untouched files, no work.
- **Forms write back.** An app with a **user-writable** pipeline accepts input. Fields are validated against the pipeline's schema, then written into its collections. Bad input gets a clear error, not a silent no-op.

Everyone opening the app reads the same live collections. There is no regenerate-and-resend step.

For scripting, invoke a code pipeline directly: [GET /api/apps/{app_id}/pipelines/{name}](endpoint:GET /api/apps/{app_id}/pipelines/{name}).

---

## When something breaks

Someone renames the *stage* column in `job-applications.csv`. The next run fails. Then:

1. The failed run surfaces in the health panel, with its error.
2. The repair policy starts a maintainer run. It reproduces the break with a dry run, touching nothing durable.
3. It fixes the pipeline and resubmits. Versions are append-only, so the fix stacks on history.
4. The next scheduled fire refreshes normally. Freshness returns to fresh.

Auto-repair fires only for scheduled runs. A manual invoke that fails is ledgered and visible, but never starts a repair run.

Your levers:

- **Rollback** to any earlier version.
- **Pause** stops the schedule with the app; paused reads as paused, not stale. **Resume** restores the cadence.
- **Archive** retires the app.

---

## What refreshes cost

| What ran | What it costs |
|---|---|
| Scheduled code refresh | Free. Sub-second, no model call. |
| Read-through, sources unchanged | Nothing runs at all. |
| LLM step inside a code pipeline | Only on changed input, within its declared budget. |
| Agentic pipeline | A model run per fire, by design. |
| Repair | Model spend only on actual breakage. |

Default to code pipelines and read-through. Reach for the model only where the work needs it.

---

## On Android

The Android client renders the same served app against the same collections. Reads, filters, and freshness behave identically. Form write-back is best done from the web console.

---

→ [Agentic Apps](index.md): the overview. → [Building an App](building.md): change what the app is made of.
