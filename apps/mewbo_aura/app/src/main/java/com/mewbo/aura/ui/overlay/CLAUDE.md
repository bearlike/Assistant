> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Assist Overlay — ui/overlay/

Scope: `ui/overlay/` — `AssistOverlayScreen` (the `VoiceInteractionSession` content view) and its
debug host `AssistOverlayPreviewActivity` (`app/src/debug/.../ui/overlay/`). Spec: Gitea #181 v5 —
voice-first, first-turn-in-overlay, second-turn-onward hands off (`voice/CLAUDE.md` owns the state
machine; this file owns the render of `AssistUiState`). "Never fork chat rendering" (root
`ui/CLAUDE.md`) is binding here same as everywhere else.

## Response card contract (Gitea #181 P3)

The `Streaming` state's first-turn response renders as a floating `ResponseCard`, recovered from
the pre-v4 `ResponseSheet` recipe (`git show f7e505a:.../ui/overlay/AssistOverlayScreen.kt`) but
with measured geometry replacing the old full-bleed 0.75-height sheet:

- Side margin **14dp** (`AuraSpacing.ResponseCard.sideMargin`) — tighter than the composer pill's
  own overlay margin (38dp, see `ui/composer/CLAUDE.md`); the card and pill are visually distinct
  widths by design, not a shared token.
- Corner radius **20dp** (`AuraShape.radiusCard`) — distinct from the 28dp bubble radius.
- Gap to the composer pill above it: **11dp** (`AuraSpacing.ResponseCard.gapToPill`).
- Drag handle **27×1.5dp** (`dragHandleWidth`/`dragHandleHeight`), 5dp ink offset from the card's
  top edge; a dedicated 32dp-tall touch strip pinned to `Alignment.TopCenter` (NOT
  `minimumInteractiveComponentSize`'s symmetric expansion, which would bleed the hit area above the
  card's rounded top corner) — swipe-up past a 24dp threshold fires `onExpand`.
- Height cap **65%** of the overlay's own measured height (`AuraSpacing.ResponseCard.maxHeightFraction`),
  with internal scroll beyond that via the shared `ChatTranscript`.
- **The card yields to the IME** — it carries `Modifier.weight(1f, fill = false)` in the bottom
  `Column` (which is `Arrangement.Bottom`-packed because a weighted child expands the Column to
  full height). Measurement-order law: pill/error/chip measure FIRST, the card gets exactly the
  leftover — so when the keyboard opens (`imePadding` shrinks the column's inner max), the CARD is
  what shrinks while the composer stays pinned above the keyboard. The 65% `heightIn` cap alone is
  computed against the FULL window height, which never changes with the IME (static session-window
  frame, `voice/CLAUDE.md`) — relying on it alone overflowed the stack. Same law as the
  composer-options sheet squeeze fix: weighted content yields, fixed chrome never gets coerced.
- **Shrink-wraps short replies** (Gitea #181 item 1, `4f8e081`): content is `ChatTranscript(...,
  fillParent = false, ...)` — the ONE additive parameter that lets the card's `LazyColumn` size to
  its own laid-out content within the `heightIn(max = ...)` cap instead of always forcing the full
  slab. `fillParent` defaults `true` (byte-identical behavior for every OTHER caller, none of which
  pass it); the overlay is the only `false` caller. **Never fork `ChatTranscript` to get a
  different sizing mode — extend the shared component's parameter surface instead**, exactly as
  this one commit did.
- Content is the SAME shared `ChatTranscript` every other surface renders — only the container
  chrome (drag handle, controls row) is local to this file. Thumbs/share controls are deliberately
  absent (no backend semantics for either yet — a documented deviation, not an oversight; don't add
  them without a backend contract to back them).
- Swipe-to-dismiss is scoped ABOVE the card's reserved region while one is showing (`1f -
  maxHeightFraction` of screen height) — a whole-screen catcher competes with the card's own
  `ChatTranscript` scroll (this exact regression was fixed once already, pre-v4; don't reintroduce
  a whole-screen catcher).

## No orb in the overlay (deleted, user decision on #181's trail)

The floating/docking overlay orb (`OverlayOrbLayer`, D-1's "branded signature") was DELETED
outright. Reasons, all evidenced: the reference overlay has no floating orb (four real-device
captures — pill + card only); the orb's window-coordinate anchor-chasing was the overlay's top
defect source (the 5/5 stranded-orb IME bug, the 300-600ms empty dock-slot gap, the desaturated
docked render, a tap-swallowing docked slot); and ~120 lines of the most fragile code in the tree
existed only to animate it. The brand mark lives in the response card's clay-flower typing
indicator and the app surfaces (launcher icon, `AuraSpark`) instead. Do NOT reintroduce a floating
overlay element that tracks another composable's `boundsInWindow()` — if a future design wants a
shared-element morph here, place both endpoints in the SAME layout tree so the layout system owns
the geometry.

## Draft-clear-on-Idle law (the mic-invisibility root cause, Gitea #181 item 3)

The `VoiceInteractionSession` composition SURVIVES a hide→show cycle (confirmed against AOSP
`VoiceInteractionManagerServiceImpl`: `mActiveSession` is reused, `onNewSession` fires exactly
once per session lifetime — see `voice/CLAUDE.md`'s session-reuse lifecycle law). Concretely: type a
partial query, dismiss WITHOUT sending, re-invoke the assistant — the SAME composition (and its
`rememberSaveable var draft`) comes back. Nothing in `AssistTurnMachine` ever touches that
Compose-local `draft` state (the machine only knows about `AssistUiState`, not the UI's draft
field), so a stale non-blank draft silently survived into the next invocation, resolved
`ComposerState.Typing`, and permanently replaced the mic circle with a ghost send-arrow — read by
users as "the mic is randomly missing." Fix: `LaunchedEffect(state is AssistUiState.Idle) { if
(state is AssistUiState.Idle) draft = TextFieldValue() }` — clears the draft every time the machine
settles back to `Idle` (dismiss/hide), piggybacking on a state value that already exists rather than
adding new machine surface. **Any new Compose-local state introduced in this file must ask the same
question**: does it need an equivalent Idle-keyed reset, given the composition can outlive any
single invocation?

## Trailing-accessory 2-way (Gitea #181 item 2; dock slot deleted with the orb)

`FloatingComposerBar`'s `trailingAccessory` override (active only while `ComposerState.Idle`) picks
ONE of two things, never a 3rd without extending this list:

1. **Bare mic glyph** (`BareMicButton`) — once `Streaming.done == true`: the post-turn
   voice-follow-up affordance, an OUTLINE glyph per reference-capture ground truth, NOT the filled
   circle (that's `Ready`'s own trailing slot only — confusing the two is a design regression).
2. **Filled mic circle** (`ComposerOverlayMicCircle`) — every other `Idle` case (fresh `Ready`;
   disabled during the brief pre-first-token `Sending` window, when the whole composer is disabled).

While a turn is actively generating (`Streaming && !done`), the composer resolves
`ComposerState.Streaming` and AuraComposer's own default cluster renders the STOP tile (wired to
`onStopStreaming`) — reference frame 3's in-pill stop square. Both accessories wire to the SAME
`AssistOverlayCallbacks.onStartListening` — the caller (`AuraSession`) decides the `RECORD_AUDIO`
grant question once, centrally; this screen never branches on permission state itself.

## Pill pull-up → app handoff (user directive 2026-07-04)

Swiping the floating composer pill UP (past `PILL_PULL_UP_THRESHOLD_DP`, 48dp — local constant,
flagged for `AuraSpacing` tokenization) hands off into the full app from ANY overlay state.
Anatomy: `detectVerticalDragGestures` on the pill root drag-follows the finger via an `Animatable`
offset; releasing past threshold commits (settle haptic), below it springs back on
`AuraMotion.composerMorphSpring`. Routing is a TESTABLE seam, `AssistTurnMachine.pullUpToApp(draft)`
(never decided in the composable): a sessionId from THIS invocation → the existing
`EXTRA_HANDOFF_SESSION_ID` route-to-session path (v4/#178, same one `expand()` uses); no session →
`EXTRA_HANDOFF_NEW_CHAT` (the app's default landing IS new chat). A typed draft rides
`EXTRA_HANDOFF_DRAFT` → chat destination `SavedStateHandle` (`HANDOFF_DRAFT_KEY`, raw value — NOT a
route arg, arbitrary text isn't URL-safe) → `ChatViewModel.seedHandoffDraft` → one-shot apply in
`ChatSurface` that deliberately does NOT tag the pending send Voice (a masqueraded Voice tag would
trigger speak-along on a typed draft's reply — its own seam, not a ride on `DictationState.Final`).
The drag offset carries an Idle-keyed `snapTo(0f)` reset per the draft-clear law above.

## IME: the platform pan is the ONLY keyboard mechanism — never add imePadding here

The WindowManager force-pans `TYPE_VOICE_INTERACTION` windows for the IME regardless of app-side
softInputMode (measured: `ADJUST_NOTHING` set both pre-attach and via live `attributes`
reassignment never changed dumpsys' `adjust=pan`, and the surface visibly panned). Pan alone parks
the focused composer directly above the keyboard — the correct, tight fit. Adding `imePadding()` on
top made BOTH mechanisms lift the content: the composer floated ~900px above the keyboard (measured
via uiautomator + `dumpsys input_method`, the "large gap" bug). The bottom `Column` therefore has
deliberately NO `imePadding()`. The in-app chat is the OPPOSITE: an Activity window where
`adjustResize` is inert under edge-to-edge and `ChatSurface` NEEDS `imePadding()` — don't
cross-apply either rule.

## Real-session-vs-preview geometry note

The real `VoiceInteractionSession` window's on-screen geometry differs from
`AssistOverlayPreviewActivity`'s (the debug fast-loop host) — the composer has been measured sitting
~1000px lower in device coordinates on the real session window than in the preview activity. Before
`decorFitsSystemWindows`/`ADJUST_RESIZE` were set explicitly (`voice/CLAUDE.md`), the two hosts had
DIFFERENT unconfigured platform soft-input defaults, which is the leading explanation for that
offset — now that both request the same explicit regime, re-verify this gap on a real device before
assuming it's fully closed. When driving the real session via `adb input tap`, always measure
coordinates from an actual screenshot of the SESSION window, never reuse preview-activity
coordinates (`voice/CLAUDE.md`'s own verification-loop guidance).
