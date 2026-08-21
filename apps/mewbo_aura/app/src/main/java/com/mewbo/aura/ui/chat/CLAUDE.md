> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md) · children: [toolcards](toolcards/CLAUDE.md) · [widget](widget/CLAUDE.md)

# Aura Chat Surface — ui/chat/

Scope: `ui/chat/` — the docked chat surface and everything it hosts: `ChatScreen` (glow owner),
`ChatSurface` (docked host), `ChatTranscript` (the ONE shared transcript tree), `ChatViewModel`,
`ChatUiState`, message rows, `ModelPickerSheet`/`ModelProviderIcons`, `MessageActionsSheet`,
`DictationDecision`/`SendDecision`/`SessionBinding`, `ChatIcons`. **Canonical visual laws:
[`DESIGN.md`](../../../../../../../../../DESIGN.md); shader math: [`ui/aurora/CLAUDE.md`](../aurora/CLAUDE.md).**

## The rendering path

- `LazyColumn(reverseLayout = true)`; items come ONLY from `TranscriptReducer` output
  ([`data/model/CLAUDE.md`](../../data/model/CLAUDE.md)) — never hand-assembled. Item spacing is
  asymmetric by chronological-predecessor type, never a uniform `Arrangement.spacedBy`.
- Assistant text — streaming AND finalized — renders ONE path (`MarkdownMessage` +
  `MarkdownBuffer.sanitize` + `rememberStreamedText`, all in [`ui/common/`](../common/CLAUDE.md));
  `WordFadeText` is deleted. Smoothness is the ABSENCE of churn (retainState + ~20Hz throttle), never a
  per-word reveal.
- **Compose stability is measured, not claimed** — `@Immutable` on `ChatItem`/`ChatUiState` is
  load-bearing here; the ui-wide rule and its three known breaks live in [`ui/CLAUDE.md`](../CLAUDE.md).
- Turn separation is BORDER + space (a `HorizontalDivider(outlineHairline)` in the `Turn` band before
  each user bubble with a predecessor); the disclaimer is a transcript-level SIBLING at DSL position 0
  (visual bottom under `reverseLayout`), gated on `shouldShowDisclaimer(hasSettledReply, isRunLive)` =
  `hasSettledReply && !isRunLive`, NEVER first-turn-anchored or mid-transcript (DESIGN.md).
  **The `!isRunLive` half is load-bearing:** on a follow-up turn a prior reply is already settled, so
  gating on `hasSettledReply` alone left the disclaimer pinned under the fresh user bubble + spark
  during the new turn's stream, ahead of its response. Gated this way the disclaimer is
  MUTUALLY EXCLUSIVE with the live spark (spark ⇔ live, disclaimer ⇔ settled), so its SOLE predecessor
  is the settled `ActionRow` footer — `ActionRow.disclaimerGap` stays `0.dp` (the footer's 48dp
  touch-cell inset supplies the air), and the spark carries ONLY its own `top = ActionRow.topMargin`
  gap (the `LazyColumn`'s bottom contentPadding buffers it off the composer; the old
  `bottom = Composer.gapTight` compensation is gone).
- **All transcript rows share ONE `Modifier.animateItem` transition** (`transcriptItemTransition`:
  `AuraMotion.transcriptItemPlacementSpring` + fades, reduced-motion-gated) so a live turn's
  mount/shuffle/unmount reflows instead of flickering. Chip ENTRANCE stays owned by
  `ChipEntranceAnimator` and the streaming text is never per-delta-faded — content rows pass
  `fadeEdges = false` (placement + removal only); the spark/disclaimer pass `true` (fade both edges).
  DESIGN.md.

## Liveness — `ChatScreen` is the sole glow owner

`ChatScreen`'s bottom `AuroraEdgeGlow` (`IN_APP_GLOW_*` tuning) is the ONE liveness layer; `ChatSurface`
renders no aura of its own. `AuraSpark` at the transcript bottom is the persistent cue for the whole
`Sending`/`Streaming` phase. The fresh-invocation ambient breathe runs
`AMBIENT_INVOCATION_WINDOW_MS = 30s`, then fades to `Hidden` — rest is solid. **That SAME bounded
breathe is re-armed on every run COMPLETION**:
the pure, unit-tested `chatGlowState(...)` shows `Listening` when EITHER the fresh-invocation
window is open on an empty transcript OR a completion linger is armed — a monotonic `completionArmToken`
captures each transition into `RunPhase.Done`, and (unlike the fresh breathe) this arm is deliberately
NOT empty-gated, because it fires precisely when a response lands into a non-empty transcript. At run-end
the glow blooms Thinking→Listening and lingers; the ≥3s ease-off applies at the linger's END (DESIGN.md
). Shader/ease-off/speed-scale laws live in [`ui/aurora/CLAUDE.md`](../aurora/CLAUDE.md).

## The project readout — stated exactly once, by whichever surface is up

Two surfaces answer "where am I working", and `ChatUiState.showsComposerScopeIndicator` is the ONE
predicate both read so they can never both answer or both stay silent:

- **Pre-session** (no session, empty transcript): `ChatSurface`'s `ComposerScopeIndicator` — project
  + faceted tool counts, tappable through to `ComposerOptionsSheet`.
- **Open session**: `ChatTopBar`'s `TopBarProjectLine`, a second line under `Mewbo <model>` — scope
  glyph + `ComposerScope.activeProjectLabel` in `caption`, and **deliberately NOT interactive** (the
  composer's "+" already edits scope, and a control here would need a 48dp touch cell inside a 32dp
  title block). Absent entirely when the session has no project, so an ordinary temp-dir chat keeps
  a one-line bar.

The line exists because the project MOVES: a session can start in auto-select mode and the model
re-points it with `switch_project`, so the project the turn opened in is not necessarily the project it
is working in. It is driven off `composerScope.selectedProjectKey`, which `ChatViewModel.publish`
re-points on every persisted `context` event
([`data/model/CLAUDE.md`](../../data/model/CLAUDE.md) § the session's project is live) — read once at
session load it would go stale mid-turn, which is precisely when it matters.

`ProjectRowKind` ([`ui/common`](../common/CLAUDE.md)) owns the glyph+tint for the three kinds
(Saved / Temporary / Auto), shared by this line and BOTH project pickers.

## The composer scope row (pre-session) — ChatSurface

`ComposerScopeIndicator` shows `project · N tools` above the composer while no session exists; tap opens
`ComposerOptionsSheet`.

- **Alignment** uses `AuraSpacing.Composer.scopeRowStartInset` — the anchor and why it is
  `horizontalMargin + height/2` live in [`ui/composer/CLAUDE.md`](../composer/CLAUDE.md).
- **Faceted counts:** the row's tool count is `ComposerScope.toolsFacetSummary`
  ([`data/model`](../../data/model/CLAUDE.md)) — "2 project · 5 system · 3 plugin" — falling back to the
  plain total when no active tool carries a recognized scope (older backend), never a lone "N other".
- **Width ladder — truncation is a LAST resort:** `ComposerScopeIndicator` pre-measures
  (`BoxWithConstraints` + `rememberTextMeasurer`) and degrades in three rungs: (1) both at natural
  width; (2) the facets collapse to the compact total `ComposerScope.toolsCompactSummary` ("N tools");
  (3) only THEN is the project name `widthIn`-capped and allowed to ellipsize. Text truncation is the
  third rung, never the first.
- **Glyphs + tint:** `ChatIcons.ProjectScope`/`ToolScope` (Folder/Build, `Icons.Filled.*`), tinted
  `scopeProject`/`scopeTool` (DESIGN.md) — glyphs only, never body text.
- The tool picker (`ComposerOptionsSheet`, [`ui/composer`](../composer/CLAUDE.md)) is provenance-GROUPED:
  scope-section headers layered over the existing per-server groups (the old per-server scope tag was
  removed to avoid double-labeling). The project picker (`ProjectPickerSheet`, [`ui/settings`](../settings/CLAUDE.md))
  marks Temporary with `ChatIcons.TemporaryProjectScope` (Schedule) + a divider.

## Model provider icons (`ModelProviderIcons`)

Model-id → provider brand glyph for `ModelPickerSheet` rows. **A model-id prefix like `openai/claude-…`
is a LiteLLM routing artifact, NOT brand truth** — selection substring-matches the lowercased id
(`"claude" in id`), never parses the prefix. It is a hand-kept MIRROR of the console's `PROVIDER_ICONS`
(`src/utils/modelIcon.ts`); the cross-pointer comment lives in BOTH files (no shared schema → drift
risk). Icons are MIT `@lobehub/icons` MONOCHROME SVGs converted to `res/drawable/ic_provider_*.xml`
(single `currentColor` path, tintable — never the colored variants, per theme discipline); each
drawable's XML carries its upstream+license provenance comment. An unrecognized id returns `null` →
the row falls back to a `material-icons-extended` glyph, never a blank slot or a bespoke "generic"
drawable.

## Message actions + terminal state + session binding

- **Long-press a user bubble** → `MessageActionsSheet` (web-console parity; the four labels are shared
  VERBATIM with the console — renaming one is a cross-surface break). `MessageAction` owns label + glyph
  + failure copy AND its own `isAvailable` (per-action, not uniform): the three mutations share the
  `pending`/`running`/`sessionEnded` gate, retry adds `&& !steer`, **Copy overrides to `true`** (it also
  restores what the long-press displaces — `SelectionContainer`, swapped out when the gesture installs).
- **A destructive retry MUST tear the reducer down and refetch `GET /events`** — the reducer is
  append-only and SSE replay is `replay = 0` (no backlog to a late subscriber). Path:
  `resetTranscriptForReload()` + `loadHistoryAndFollow()`.
- **`SessionBinding` — one `ChatViewModel` serves every session.** An async load must be cancelled AND
  re-checked (`isCurrent(id)` at every suspension point before any shared write); the sheet target is
  cleared on session switch. **REFUTED (do not re-chase):** "after a retry's 202 `GET /events` may not
  report `running: true` yet" — `start_async` registers the run synchronously before the 202.
- **Permanent termination is a terminal COMPOSER state, not an error card** — `ChatUiState.sessionEnded`
  disables the composer and drops any Retry (a terminated session 410s every retry). No error residue
  (DESIGN.md).

## Read-aloud: `ChatViewModel` is the ONE instance that narrates a typed turn

`ChatViewModel` injects `DeviceShape` for exactly one question and hands it to its `SpeechController`
— whether a TYPED turn is read aloud (`SpeechController.narratesTurn`,
[`voice/CLAUDE.md`](../../voice/CLAUDE.md)). Two things about that seam bite:

- **The user's switch stays where it is.** `SettingsStore.speakResponses` folds into `publish()`'s
  `muted` argument and nothing else; the shape decides whether a typed turn is ELIGIBLE, the switch
  decides whether an eligible one speaks. A second gate here would give one decision two owners.
- **The shape arrives from Hilt, so `MainActivity`'s debug `-e deviceShape television` override does
  NOT reach it** — that extra only feeds `LocalDeviceShape` for the Compose tree. Read-aloud on a
  handheld dev device follows the real platform answer; use a television target to exercise it.

## Did the run fail? `error` alone — `last_error` is NEVER a failure

The completion payload carries two error fields and they mean different things. `error` is set solely
on the orchestrator's terminal-failure path. `last_error` is a **sticky diagnostic** recording the last
tool failure inside the run, and it survives the model recovering from that tool call and going on to
answer — so a fully successful run routinely carries `last_error` residue while `error` stays null.
**Keying failure on `last_error` surfaces an internal tool/MCP error as a session-level failure**, under
a complete and correct reply.

One decision, spelled in THREE places, and all three must agree — a card without a phase, or a phase
without a card, is one surface disagreeing with itself about whether the turn worked:

| Site | Decides |
|---|---|
| `completionPhaseFor` (`ChatViewModel.kt`, top-level `internal`) | `RunPhase.Done` vs `RunPhase.Error` — the composer + spark |
| `TranscriptReducer.foldCompletion` ([`data/model/`](../../data/model/CLAUDE.md)) | whether a `ChatItem.ErrorCard` is spawned |
| `RunNotificationController.completionNotice` ([`notify/`](../../notify/CLAUDE.md)) | `Done` vs `Failed` on the notification |

**This has already drifted once:** `completionPhaseFor` read `error != null || lastError != null` while
the other two read `error` alone, so a recovered tool call flipped an otherwise-successful chat into
`RunPhase.Error`. `ChatCompletionPhaseTest` is the symmetric mirror of
`RunNotificationControllerTest`'s `completionNotice` suite, and additionally composes the phase
decision with the REAL reducer so the card and the phase are asserted together. When `error` IS
present the reducer's rendered MESSAGE still falls back to `last_error` — that fallback is about which
string to show, never about whether the run failed.

**A `stream_error` is not a run failure either — it means "you are no longer seeing this run."** The
client detaches; the run continues server-side and re-opening the session replays it to completion.
A transient drop never reaches here at all: `SessionStreamClient` swallows `IOException` and reconnects
with `?after=` ([`data/sse/`](../../data/sse/CLAUDE.md)). Note the assist overlay additionally guards
its `StreamError` branch on `!alreadyDone` (`AssistTurnMachine`) so a late transport failure cannot
un-finalize an already-completed turn; `ChatViewModel.subscribeLive` has no such guard. **Unverified:**
no reachable sequence was found that materializes a `StreamError` after a `completion` — do not "fix"
this without a repro.

Promoted tool cards → [`toolcards/CLAUDE.md`](toolcards/CLAUDE.md); the Streamlit widget card →
[`widget/CLAUDE.md`](widget/CLAUDE.md). `ChatIcons` is the FROZEN legacy hand-rolled glyph set — reuse
existing glyphs, but new glyphs pull from `material-icons-extended` first (app-root CLAUDE.md § Iconography).
