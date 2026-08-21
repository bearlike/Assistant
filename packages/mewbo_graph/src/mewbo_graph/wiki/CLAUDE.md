> ↑ [packages/mewbo_graph/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# MewboWiki indexing job — the durable state machine (engine side)

The seven-phase indexing lifecycle as owned by THIS library: `types.py` (`IndexingJob`,
`IndexingPhase`, `IndexingStatus`), `resume.py` (`ResumePlan`), `store.py` (`PageClaim`,
`claim_job_page`, the commit-scoped counts), and the plugin-side emitters in
`plugins/wiki/_ctx.py`. How a process restart DRIVES this state machine — `JobRecovery`,
`WikiResume.resume`, session-end hooks, the retry cap — is
`apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` → "Restart durability".

## The seven phases

```
clone → scan → graph → enrich → plan → pages → finalize
```

`PHASE_SEQUENCE` makes that order readable so "is this a forward move" has one answer. Index
position is its only meaning, so **never persist or wire an ordinal** — inserting a phase would
renumber every stored value. `IndexingJob.regresses_to(phase)` is the one place the order is
ASKED rather than re-derived: a resume legitimately re-runs `clone`/`scan` on a job that
already reached `pages`, and without the guard both progress surfaces read that as the bar
walking backwards.

## `status` vs `phase` — `status="scanning"` spans four phases BY DESIGN

`IndexingStatus` is the COARSE bucket
(`queued|scanning|finalizing|interrupted|complete|cancelled|failed`); `IndexingJob.phase` is
the fine-grained state. They are written by different code on different cadences: `clone` is
the ONLY writer of `status="scanning"` (stamped once, at the END of the phase), `commit_plan`
the only writer of `status="finalizing"` (at the END of `plan`'s boundary tool), `finalize` the
only writer of `status="complete"`.

So `status="scanning"` covers everything from the end of `clone` through `scan`, `graph`,
`enrich` and most of `plan`, and `status="finalizing"` covers the rest of `plan`, all of
`pages` and the start of `finalize`. **`status` sitting at `"scanning"` for a long time is not
evidence of a stuck job** — read `phase` plus
`phase_progress_current`/`phase_progress_total`/`last_progress_at`, which carry live position.

## What is durable at each phase, and what a resume redoes vs skips

| Phase | Durable artifact | Resume behaviour |
|---|---|---|
| `clone` | `commit_sha`/`branch` stamped on the job | Always re-runs — cheap, and the source must be back on disk before `pages` can write anything |
| `scan` | `FileManifest` rows; `scanned_count`/`total_count`/`current_file` | Always re-runs — same reason |
| `graph` | Nodes/edges/embeddings, attributed `(slug, commit_sha)` | Skipped when `ResumePlan` counts a non-zero node count FOR THIS COMMIT |
| `enrich` | Abstract entities, same attribution | Skipped when the commit-scoped entity count is non-zero |
| `plan` | The committed page-plan list | Skipped when the job already has a committed plan |
| `pages` | Each page's `PageClaim` + the page, attributed `(commit_sha, job_id)` | Decided PER PAGE — only pages absent from `pages_done` are re-written |
| `finalize` | `Project` row, `status="complete"`, prior-commit supersession | Always re-runs — an idempotent upsert |

`graph`/`enrich`/`plan` are the three `SKIPPABLE_PHASES` an interrupted job may reuse
wholesale. `pages` is neither whole-skip nor always-redo.

**A scoped refresh reuses `pages` for its own act phase.** The sessionless delta pass runs
`clone → scan → graph → finalize`, but the LLM act phase that rewrites stale pages emits
`"pages"` as its job phase (`plugins/wiki/scoped_refresh.py`) before `finalize`. Reusing it
rather than minting a phase avoids renumbering `PHASE_SEQUENCE`, and `regresses_to` already
treats re-entry into `pages` as forward progress.

**`finalize`'s Mermaid gate is a LINTER, not a syntax validator — size your trust
accordingly.** `MermaidValidator.review` (`plugins/wiki/mermaid.py`) encodes three measured
defect classes — a reserved keyword as a node id or participant alias, an unescaped
bracket-class character in an unquoted label, a semicolon inside a `sequenceDiagram` message —
and performs NO grammar check, because the real parser is JavaScript and this runtime has no
Node. Verified by probe: a bad diagram type (`graff TD`), an unterminated `["label`, and an
unclosed `subgraph` each returned ZERO defects and passed, while the known-bad classes were
correctly rejected. A syntactically invalid diagram therefore reaches a persisted page or Q&A
answer and surfaces as Mermaid's error box in the console. The same validator gates QA answers
through `emit_answer.py`, via `inspect` rather than `review`.

## `ResumePlan` — computed once, consulted many times, fails CLOSED

`ResumePlan.build(store, job)` is the single place that inspects the store and decides what an
interrupted job already finished. It is commit-scoped on purpose: the store holds the UNION of
every commit ever indexed for a slug, so "N nodes exist" answers "some commit built a graph",
never "THIS commit's graph is built" — the distinction the skip decision turns on. It runs ONCE
and persists as a small dict; every phase tool rebuilds it cheaply via
`ResumePlan.from_persisted` instead of re-querying the graph per invocation.

**Every read it makes fails CLOSED.** `_count_graph` / `_count_entities` / `_plan_page_ids` /
`_written_page_ids` raise `ResumeCountError` on a store exception rather than returning `0` —
"I counted zero" and "I could not count" are different answers, and only the first legitimately
selects a rebuild. A rebuild costs a full re-index plus a re-embedding pass, so inferring one
from a transient store hiccup is the most expensive mistake this module can make. The caller
that catches the refusal must not spend its retry budget on it.

## Prose is not an invariant — every skippable phase needs its own code guard

`ResumePlan.summary()` narrates the skip decision to the driving agent in natural language, and
for the tool-less `enrich` fan-out that narration is the ONLY instruction an agent gets, since
there is no boundary tool to hang a guard on. Narration enforces nothing: a model that skips
reading its own instructions redoes the work a resume exists to avoid. Every phase capable of
skipping therefore carries its OWN `resume_plan.should_skip(phase)` check inside the tool that
would redo the work — `plugins/wiki/build_graph.py`, `mint_entity.py`, `commit_plan.py`.
`pages` needs a different shape because it skips PER PAGE: `submit_page.py` checks
`page_id in rp.pages_done` before anything else.

## Claiming a page — size-of-set, and claim BEFORE write

**The count is the SIZE OF THE WRITTEN SET, never a free-running increment.** `claim_job_page`
derives `PageClaim.count` from `len(submitted_page_ids)` in one atomic call, which also stops
two racing page-writers both seeing "new". A counter that increments cannot survive a resume:
replaying the remaining 10 pages of an interrupted 40/50 job counts them `1..10` and reports
finishing at `10/50`, while adding the increment onto the persisted total double-counts to
`90/50`. Size-of-set makes both directions of overshoot structurally impossible.

**The count is never clamped to `total_pages`.** An agent that writes a page absent from the
plan is a real signal; `finalize`'s prune (`store.prune_pages`, keyed to the plan + landing
page) removes it, not the counter. Clamping would render `51/50` as `50/50` and erase the only
visible sign that a page-writer diverged from its plan.

**`get_job_page_ids` falls back to `page_ids_for_job` (the `(slug, job_id)` stamp) when no claim
record exists, because of a Mongo trap in the claim filter itself:**
`{"submitted_page_ids": {"$ne": page_id}}` MATCHES a document where the field is absent
entirely, not only one where it exists and differs. Without `_seed_claim_record` seeding the set
from attribution FIRST, a job predating the claim record claims its way up from zero while its
already-written pages sit unaccounted for, reporting a fraction of its progress and regenerating
those pages on a later resume. `$ne`-matches-absent is a driver behaviour worth its own
regression test at the next store method filtering on an optional array field.

**The claim is taken BEFORE the page is saved, and the order is load-bearing.** A job with no
claim record seeds one from page attribution, so saving the page first would put THIS page into
that seed — it would read back as already claimed and never get counted.

## Updating the job snapshot writes the NAMED fields, never the document

`update_job` is concrete on `WikiStoreBase`; both drivers implement only the write
(`_write_job_patch`). "Set these fields" and "rewrite the document that happens to hold them"
are not the same operation: a read-modify-write over every field means two overlapping writers
revert each other even when the fields they touch are disjoint. The writers here are ordinary
and concurrent by design — `PhaseProgress` on a 5s cadence, `scan` on a 50ms one, and an HTTP
thread writing `status` through `cancel_job` — so a cancel landing between a progress writer's
read and its write is silently reverted and the job goes back to running with no record that a
cancel was requested. The reverse ordering merely drops a progress update, which is why this
needs a mechanism rather than a retry.

**The rule lives once, on the base; the mechanism is per-driver, because they genuinely
differ.** `JobPatch` carries the caller's named fields after validating them against the WHOLE
job — narrowing the WRITE never narrows the CHECK, so `extra="forbid"` and per-field coercion
still fire. Then:

- **JSON** takes `self._lock` across a re-read → apply → save. The whole file is the unit of
  write, so field-scoping is only real if the read the merge is built on cannot be interleaved
  with the write that replaces it; the lock must span both, which is why that path re-reads
  rather than trusting the snapshot the base already validated.
- **Mongo** `$set`s only the named fields in one server-side update and needs no lock — nor
  could it have one, since the api runs several worker processes and a process-local lock says
  nothing about the writer next door. It returns the AFTER document (`ReturnDocument.AFTER`) so
  the caller reads what is stored rather than its own merged guess; a locally merged return
  reports a concurrent writer's landed field as reverted.

**A named `None` is a VALUE, never an omission.** `emit_phase` clears the three
`phase_progress_*` fields by naming them, so a narrowing keyed on truthiness instead of on which
fields were NAMED silently discards the write the clearing invariant depends on. This is the
opposite rule to `PROJECT_UPDATABLE`/`update_project`, whose `None` genuinely means "not
supplied" — do not copy one onto the other.

A single-threaded test cannot fail on any of this;
`tests/wiki/test_store_job_concurrency.py` parks one writer mid-update on an event and lets a
rival finish inside the window, against both drivers.

## Progress — a declared plan of steps, not a shared register

**The ledger is the model; the triple is a compatibility shim.** A phase is not
one unit of work — `graph` is nine steps with three different units, and only
three of them can be counted at all. `IndexingJob.progress` holds a
`ProgressLedger` (`mewbo_core.contracts.progress`, whose `contracts/CLAUDE.md`
owns the contract's reasoning); `plugins/wiki/step_plans.py` declares each
phase's steps as data, and `ProgressReporter` (`plugins/wiki/_ctx.py`) is the ONE
writer.

**Work runs inside a scope, and that is what makes coverage checkable.**

```python
with progress.step("graph.resolve_scip_index"):
    parsed = _apply_resolver(...)
```

Entering writes `running` plus the step's OWN origin; leaving writes `done`; an
exception writes `failed` with bounded text and re-raises, so a step is never
left running because its body threw. `emit_log` stamps the open step onto the
event, so a log line written with no step open is work outside the ledger —
`tests/wiki/test_progress_ledger_coverage.py` fails on it. That gate is the
point: the previous model's tests proved the *emitter* worked and could not fail
for an entire phase going silent.

**Why this replaced a single triple.** The `graph` phase's two writers bracketed
its dominant cost, so the parse loop's terminal `advance(N, N, force=True)` stood
through the resolver, the whole-graph validation and the edge persist. Nothing
but `emit_phase` cleared it, and that had already fired — so a reader saw a
finished count, a bar at its phase ceiling and a time-remaining of exactly zero,
for an hour. **A stale terminal count is indistinguishable from completion**, and
that is worse than silence, which at least looks stalled.

**Do not thread a counter into a blocking call to "fix" an opaque stretch.** The
resolver indexes one project root on this repository, so a per-root counter emits
a single unit and the hour stays silent. It is an uncountable step with a start
time, and that is the honest shape.

`PhaseProgress` survives as the write CADENCE of an open step (5s), not as the
progress mechanism. The `phase_progress_*` triple is still written, derived from
the active COUNTED step, purely so a job already in flight and an un-migrated
client keep working — it is no longer the model, and nothing new should read it.

**Two ledger writers can lose each other's step update; that direction is
accepted.** The enrich fan-out reaches the reporter from a fresh tool instance
per mint. A lost update is bounded and self-heals on the next write, whereas
reverting a concurrent writer's FIELD does not — which is why this stays a
named-field write and never grows a lock. See "Updating the job snapshot" above.

## The clearing invariant the triple still leans on

`emit_phase` is the ONE writer of `IndexingJob.phase`, and it CLEARS
`phase_progress_current`/`_total`/`_unit` on every transition. That clear is the invariant every
progress reader leans on: a non-null `phase_progress_current` always belongs to the phase named
in `phase`. Skipping it is exactly how `scanned_count`/`current_file` — scan's own older
per-phase fields — became untrustworthy: they freeze at scan's last value and stay readable, and
plausible, through every later phase.

`PhaseProgress` (`_PROGRESS_INTERVAL_S = 5.0`) is the "still moving" writer for `graph` and
`enrich`, the two phases that are a long loop or a fan-out with no per-unit boundary tool.
Without it nothing at all is written between their start and end — one index spent 25 minutes
inside the tree-sitter loop emitting nothing, indistinguishable from outside a dead run.

**Do not unify this with `scan`'s 50ms per-file flush** (`_FLUSH_INTERVAL_S`). `scan` already
owns an honest per-file counter pair and flushes fast enough that nobody polling reads a stale
value; routing it through the 5s throttle would either halve that cadence or double the write
count.

**A progress throttle must treat a persisted stamp AHEAD of its own clock as stale, never as
recent.** `PhaseProgress._due` adopts the job's persisted `last_progress_at` as its baseline so
a fresh fan-out worker arriving mid-interval stays silent — but only when the elapsed time is
non-negative. A NEGATIVE elapsed (clock skew, or a write from another host) is treated as
"unusable, write anyway"; reading it as "written recently, hold off" suppresses progress writes
for as long as the skew persists.

## `wiki.refresh.*` thresholds are read ONCE, at the composer

`RefreshOrchestrator.from_store` (`refresh.py`) is the only place any `wiki.refresh` threshold
is read; it passes each down as a constructor argument to the stage that consumes it —
`closure_max_depth` → `GraphDeltaIndexer`, `drift_keep`/`drift_invalidate` → `MemoryReconciler`,
`page_keep`/`page_edit`/`page_regen`/`new_page_min` → `DocStalenessPlanner`. The stages read no
config at all, which is what lets a test construct one without a config file and stops a
deployment setting silently overriding an injected value. **Config decides the DEFAULT, never
whether.**

**Why this is a rule rather than a call site.** Every stage constructor defaults its threshold to
the same number the config field defaults to, so a stage built with none of them behaves
*byte-identically* to a correctly wired one on a default deployment — which is how all seven
knobs can ship declared, documented and read by nothing, with a green suite and no symptom, an
operator retuning one seeing no error and no effect.
`tests/wiki/test_refresh_config_coverage.py` therefore enumerates
`WikiRefreshConfig.model_fields` and proves at RUNTIME that each arrives on a stage; a
hand-listed set of names passes forever the first time an eighth field is forgotten.

Two traps:

- **Three fields are RENAMED on the way down.** `page_keep`/`page_edit`/`page_regen` become
  `DocStalenessPlanner(keep=…, edit=…, regen=…)`. A name-matched check reports those as unwired
  while they work correctly, and a `**config`-style splat fails outright — match by the value
  that arrives, not the name it arrives under.
- **Grep is the wrong instrument for asking whether a field is consumed.** The reader passes the
  field name as a VARIABLE, so searching for the literal string reports every field as unwired.
  Build the composer and read the value off the stage instead.

**`from_store` also composes two collaborators that are NOT thresholds and do not obey that
rule.** `resolver` gates itself on `ScipPythonResolver.is_available()`, and `on_report` defaults
to the module logger — `scoped_refresh.py` passes its own `emit_log` so the "which leg ran" line
lands in the JOB timeline beside the refresh it describes rather than only in a process log
nobody correlates. Do not add a config switch for either: nothing an operator could set changes
what the binaries' presence already decides, and a knob would give a deployment a way to
silently turn the leg off. They stay out of the coverage test's enumeration because that test
proves every `WikiRefreshConfig` FIELD reaches a stage, and these are not fields.

## The delta path resolves symbols too — `ScopedEdgeResolver`

A scoped refresh re-parses only the files a diff touched, and a raw tree-sitter re-parse emits
cross-file edges BY NAME. Persisting those is destructive, not neutral: the full `graph` phase
already replaced the same edges with the resolver's exact ones. See
`packages/mewbo_graph/CLAUDE.md` → "Faithful symbol resolution" for the measured decay and the
shared `_apply_resolver` seam.

- **The resolver sees the WHOLE project; only its OUTPUT is scoped.** A reference in a dirty
  file usually points at a definition in a file the diff never touched, so handing the resolver
  the dirty nodes alone resolves those to nothing — the same edge loss in a cheaper disguise.
  `GraphDeltaIndexer._universe` assembles the re-parsed dirty nodes ∪ the store's nodes for
  every untouched file (dirty ∪ deleted excluded, so the resolver is never offered two
  definitions of one symbol), and `ScopedEdgeResolver.resolve` keeps only edges whose SOURCE
  file is dirty — the only files whose edges `apply` retracted. An untouched file's edges stay
  as the last full index left them.
- **Edge writes are deferred into ONE batch after resolution**, because which edges survive is
  not decidable per file. Nodes still go per file — the resolver reads them and never rewrites
  them.
- **The early-cutoff signature is read off what was PERSISTED, never off the raw parse.**
  `_sig_by_file` is the ONE attribution rule shared by the pre-state (stored edges) and the
  post-state (the edges about to be stored). Comparing a raw parse against a resolved store
  makes the two sides incomparable for every resolved file and disables the cutoff wholesale.

Both gates — no resolver injected, or a dirty scope in no language the resolver supersedes
(`ScopedEdgeResolver.covers`) — are checked BEFORE the universe read, because that read is
`O(collection)` in the project's stored nodes and a markdown-only refresh must not pay it, let
alone run pyright. The leg's own cost is `O(repo)`, offline-class work belonging to a refresh
JOB. A resolver that raises degrades to the raw edges with a warning rather than failing the
refresh: a name-matched graph is worse than a resolved one and far better than an interrupted
refresh.

## `DocPageNote.anchor_keys` are whole file paths, not symbols

`anchor_keys` is typed `list[EntityKey]`, the SAME string type a code-symbol anchor uses
(`path#Symbol`), but `DocStalenessPlanner._anchor_keys` never emits the `#Symbol` half — it
reads `page.frontmatter.relevant_sources` and keeps only each entry's `path`, deduplicated.

So `_assess`'s `0.5 · direct` term is a fraction of a page's cited FILES that changed, not of
its cited symbols. A page anchored to five files where one had an unrelated one-line edit scores
identically to one where that file's every symbol the page discusses was rewritten. It is also
why a rewritten page's verdict is RESET rather than re-scored: re-scoring buys little when the
intersection is already quantised to files. **Do not read `EntityKey` here as a promise of
symbol-level precision.**

## Entity resolution under resume

`enrich`'s resolution decisions are NOT deterministic under replay — a property of the
`entities/` resolver scoring against live store state, not of this job's checkpointing. See
`packages/mewbo_graph/CLAUDE.md` before assuming a resumed `enrich` reproduces the original
run's entity ids.
