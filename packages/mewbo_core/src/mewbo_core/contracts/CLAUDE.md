> ↑ [packages/mewbo_core/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# `contracts/` — the modules that import nothing from core

Scope: `types.py` · `errors.py` · `defaults.py` · `run_error.py` ·
`verification.py` · `secret_redaction.py` · `diff_stat.py` · `progress.py`. What
groups these is not subject matter. It is a structural property, and the property
is load-bearing.

## The zero-core-import law

**Nothing in this package may gain a module-top import from elsewhere in
`mewbo_core`.** `config.py` declares its field defaults FROM here, and `config` is
imported by nearly every other module, so an upward import added here closes a cycle
through config. It does not fail at the edit: it fails as an `ImportError` raised
from an unrelated module whose import order happened to change, which is a much
longer walk back to the cause.

`defaults.py` is what severs it — the retry/fallback constants and the verification
timeout live in a module that imports nothing, and `llm_resilience` and
`verification` re-import them, so every consumer still reads a constant from the
module whose behaviour it describes and
`from mewbo_core.llm.llm_resilience import DEFAULT_TIMEOUT` still resolves.

**A `TYPE_CHECKING` guard is not the exception to reach for here.** It would be safe
at runtime, but the whole value of this package is that a reviewer can check its
property by reading the import block; a guard makes that one more thing to be right
about.

`verification.py` holds the SPEC, not the gate: `CommandVerification` is a
data-owned discriminated union like `TriggerSpec` — it validates argv, interprets a
result, and does no I/O. WHEN it runs is the completion seam's decision
(`loop/CLAUDE.md`). `secret_redaction.py` is a module-level singleton
(`SecretRedactor`) with no I/O, installed at the loguru sink; it belongs here
because a redaction path that had to import config would be unusable from config
itself.

**Do not reintroduce a root-level module whose name collides with a stdlib one.**
`mewbo_core/types.py` at the package root shadows the stdlib `types` for any process
whose cwd is that directory: `python3 -c "import ast"` dies with `ImportError:
cannot import name 'MappingProxyType' from partially initialized module 'types'`.

## Bounded failure emission (`run_error.py`)

`RunError` is THE seam for classifying and bounding a run failure. One atomic
Pydantic model, `extra="forbid"`, with the classification table, the markup guard
and every length clamp as members validated AT DEFINITION — so no call site can
construct an unbounded or markup-bearing value, not even a direct `RunError(...)` or
a `model_validate` of a stored payload. It imports no I/O: the exception and the
model name arrive as ARGS.

It exists because a run that dies inside the LLM call chain otherwise surfaces as
one flat, UNCAPPED string — a live session emitted 5,887 characters when LiteLLM
embedded an upstream HTML error page into the exception message, which then rode the
payload into every client and into the next run's `recent_events`.

- **THE LAW: `kind` and `title` derive from the exception TYPE and the error-class
  NAME, NEVER from the response body.** Every message read funnels through
  `_body_free_head`, which cuts at the first `<` — that is what makes the rule
  STRUCTURAL rather than conventional, since an error-class name can never contain
  `<`. An HTML body additionally forces a SYNTHESIZED title, with the status taken
  from the derived `kind`. The reason is not tidiness: an upstream error page's own
  `<title>` routinely names the operator's internal infrastructure, and these
  payloads are persisted and replayed to every client on every history read.
- **An empty `str(exc)` is a distinct edge case this seam does NOT cover.** Several
  exception classes stringify to `""` (`TimeoutError` costs the most forensic
  effort). `RunError.from_exception` has no guard: `message=str(exc)`, so `title`
  falls to the generic `_FALLBACK_TITLE` rather than naming the exception type.
  `kind` survives regardless — `_chain_class_names` walks `type(exc).__name__`, never
  the message — so misclassification isn't the risk; an uninformatively generic title
  is. The type-name-on-empty fix lives one layer up, in
  `LlmResilienceExhausted.describe_error` and its Langfuse twin
  `components.py:_span_status_message`. **Copy that pattern for a new error surface
  rather than assuming `RunError` covers it.**
- **Four emission paths must ALL stay bounded — a payload-only cap is not enough.**
  (1) `completion.error`/`last_error`. (2) `error_msg` forwarded to `on_session_end`
  hooks: its consumers post it as a forge PR comment, as a chat message, and persist
  it on a pipeline run, so it is OUTWARD-facing and must never carry provider markup.
  (3) `task_queue.last_error` on the STICKY branch
  (`halted_no_progress`/`max_steps_reached`/`verification_failed`), not only the
  raising one — the CLI and the scg map-job read that ATTRIBUTE directly rather than
  the wire payload. (4) Sub-agent failures, bounded at the child in BOTH `_spawn_one`
  and `_run_child_lifecycle`, so a raw provider exception cannot ride into the
  parent's context.
- **Three projections, three jobs — pick by consumer.** `title` is a one-line,
  markup-free label for a card header, and is also the attestation `done_reason` (a
  short-label cap that a raw `str(exc)[:200]` slice could fill with the head of an
  HTML page). `brief()` is the bounded blurb written to the flat `error`/`last_error`
  keys the non-console clients render. `detail` is the full 10K-capped diagnostic
  behind an expandable view. **`brief()` degrades to `title` ONLY for an HTML body** —
  that carve-out keeps an informative failure informative: a rate limit still reads
  as one, while the 5,887-character page becomes 42 characters.
- **`error_detail` is ADDITIVE.** It rides alongside the flat keys rather than
  replacing them, so a consumer that only knows `error`/`last_error` is unaffected.
  Aura's decoder is `ignoreUnknownKeys = true` at every decode site
  (`di/DataModule.provideJson`), so a new payload key is safe there.

## Declared work plans (`progress.py`)

`StepSpec` is the plan (code), `StepRecord` the observation (state),
`ProgressLedger` the export (wire). It lives here because three long-running
subsystems project into it and the DAG only flows down; it imports nothing but
pydantic and stdlib, and the clock arrives as an ARGUMENT to every method that
needs one.

**The defect it makes unrepresentable.** A single `(current, total, unit)` triple
on an operation's record is a shared register with no owner and no lifetime.
Whoever wrote last holds it until someone else writes, so a loop that finished at
`921/921` keeps asserting `921/921` through every later step that reports
nothing — and a reader computing `current/total` paints a *completed* bar, with a
time-remaining of exactly zero, over work that has an hour left. That is strictly
harder to notice than a stalled bar. Records have an owner and a lifetime by
construction, so the same state cannot be spelled.

- **A step with `unit=None` is UNCOUNTABLE and still has a start time.** The cure
  for an opaque stretch is an OPEN STEP, not a counter: a blocking subprocess
  cannot supply a denominator, but "Resolving symbols · running 47m" is already
  the honest answer. Threading a counter through such a call is the fix that
  looks right and changes nothing.
- **`fraction()` is monotonic by construction** — a terminal step holds its full
  weight forever, a pending one contributes nothing — so a bar built on it cannot
  walk backwards when a phase changes unit.
- **`eta_seconds` measures a rate over the WHOLE operation**, so a step boundary
  has no discontinuity to explode at. A rate measured from the operation's start
  divided by a fraction describing only the CURRENT step blows up as that
  fraction approaches zero, which is exactly when a reader first looks.
- **`None` and `0` are different answers and must stay so.** `None` is "nothing
  to extrapolate from"; `0` is "finished". Collapsing them is the original bug.
- **The ledger is bounded by DECLARED steps, never by units.** A record minted per
  file turns a constant-size field into one that grows with the repository, which
  is `O(all history)` on an interactive read path. Weights are RELATIVE, so a
  caller may declare hand-set defaults now and replace them with measured seconds
  later without any consumer changing.

## Diff arithmetic (`diff_stat.py`)

`DiffStat` is the ONE home for `+N -M`. It lives in core because `mewbo_tools` EMITS
the counts and the CLI RENDERS them, and the DAG only flows down. Left to their own
devices the three surfaces disagree: reconstructing old/new sides from a unified
diff and re-diffing them under-reports, because `SequenceMatcher` re-pairs lines
across hunk boundaries the diff had kept apart.

- **Counts ride WITH the diff.** `format_diff_result` stamps
  `additions`/`deletions` into the envelope, so the event log carries the number
  rather than every reader re-deriving it from a text the transcript may have
  truncated. Readers still fall back to re-tallying for documents written before
  those keys existed — **that fallback is required, not optional**.
- **⚠️ A substring test for the diff envelope HAS FALSE POSITIVES.** Gate the parse
  on it if you like, but the decision must be the PARSED `kind`: a tool that merely
  READ a diff-shaped document — a page fetch, or a code search over this repository —
  quotes the same bytes without having written a line. Measured on live data, 4 of
  431 substring hits were exactly that (`wiki_read_file`, `wiki_query_graph`,
  `web_url_read`), each of which would credit a repo-reading session with edits.
- **Two legs, envelope first, never both.** Only `file_edit_tool` and
  `aider_edit_block_tool` emit an envelope; external/MCP edit tools (`Edit`, `Write`)
  emit none, so a subordinate leg synthesizes from
  `tool_input.old_string`/`new_string` under the same predicate the console uses
  (`utils/logs.ts`) — the two are a PAIR, and a change to either predicate belongs in
  both. Shell-driven edits (`git apply`, heredocs) are deliberately uncaptured: a
  guess from a shell command's text is worse than an honest omission.
- Aggregation folds into `summarize_session`'s EXISTING single pass over the
  transcript (that function's own comment forbids a second walk), so a per-session
  rollup costs no extra I/O. `diff_stat` is appended only when non-zero, so a session
  that edited nothing keeps a byte-identical summary and a consumer can read the
  key's presence as "this session changed something".
