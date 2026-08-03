# Agentic Apps

Describe an app in one sentence. A builder agent reads your real files, derives the data model, writes a Streamlit frontend and the pipelines that feed it, then puts it online. From then on the platform keeps it fresh. You spend nothing to keep it running.

Inside the console the product is branded **Apps**. It is a peer of the Agentic Wiki and Agentic Search, built on the same orchestrator.

---

## How it works

```mermaid
flowchart LR
    D([Describe your app]) --> B["Builder agent\ngenerates frontend, pipelines, schedule"]
    B --> P["Platform runs pipelines\non their schedule"]
    P -->|"zero-token steady state"| C[("Collections")]
    C --> A["Your Streamlit app\nlive-reads every rerun"]
    P -.->|"only if a pipeline breaks"| R([Repair])
    R -.-> P
```

- **You describe.** One sentence about the data you care about.
- **The builder generates.** From your actual files it derives collections, a frontend, pipelines, and a refresh schedule.
- **The platform runs.** Pipelines fire on schedule and write collections. Most are plain code: sub-second, zero model cost.
- **Your app live-reads.** Every rerun renders the current collections, never a frozen copy.
- **Model runs return only for repair.** A failed pipeline run starts a maintainer repair run. Nothing else does.

A built app looks like this once it is live. `LLM Model Compare` filters and ranks models by capability, and its own pipelines refresh the underlying snapshots daily.

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-apps-01-detail.png" alt="A live Mewbo App called LLM Model Compare, version 1, marked Live. A left filter rail covers creator or provider, release year, capabilities, minimum intelligence index, minimum output tokens per second, and maximum blended cost. The center shows stat tiles and a bar chart ranked by Coding, followed by a ranked list. A right rail shows Health with last refreshed and next refresh time and the maintainer, Recent runs, Pipelines refreshing daily, the Cron schedule, and Versions." style="width: 100%; max-width: 960px; height: auto;" />
</div>

---

## What you get

- **A live Streamlit app**, in your browser and in the Android client.
- **Self-maintaining pipelines.** Code pipelines move and parse data deterministically. Agentic ones exist for work that cannot be expressed as deterministic code.
- **An honest health panel.** Freshness, per-pipeline schedules, and loud warnings. Silence never stands in for "fine".
- **Versioned history.** Every submit is a version. Rollback is one step.

---

## An app, not a widget

A **widget** is a snapshot: rendered once, frozen at creation. An **app** is alive: its pipelines keep running, so tomorrow it shows tomorrow's data.

Want a fixed picture? Make a widget. Want the current state? Make an app.

---

## What it costs

Building spends model effort once: a few minutes of agent time. After that:

- Code refreshes are free.
- An LLM step inside a pipeline runs only on changed input, within its declared budget.
- Agentic pipelines and repairs are the only recurring model spend.

An app that only moves and parses data costs nothing to keep alive.

---

## Where it lives

The **Apps** tab in the Mewbo Console, next to Tasks, Wiki, and Search. The same apps render in the Aura Android client. Surfaces that advertise the `apps` capability get the tab.

→ [Building an App](building.md): the create flow and what the builder does.

→ [Living with an App](operating.md): health, refreshes, versions, and repair.
