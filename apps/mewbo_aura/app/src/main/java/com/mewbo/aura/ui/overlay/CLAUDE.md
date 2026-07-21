> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Assist Overlay — ui/overlay/

Scope: `ui/overlay/` — `AssistOverlayScreen` (the `VoiceInteractionSession` content view) and its
debug host `AssistOverlayPreviewActivity` (`app/src/debug/.../ui/overlay/`). Contract: v5 —
voice-first, first-turn-in-overlay, second-turn-onward hands off (`voice/CLAUDE.md` owns the state
machine; this file owns the render of `AssistUiState`). "Never fork chat rendering" (root
`ui/CLAUDE.md`) is binding here same as everywhere else.

## Response card contract

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
- **Shrink-wraps short replies** (`4f8e081`): content is `ChatTranscript(...,
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

## No orb in the overlay (deleted, user decision on v5's trail)

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

## Draft-clear-on-Idle law (the mic-invisibility root cause)

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

## Trailing-accessory 2-way (dock slot deleted with the orb)

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
`EXTRA_HANDOFF_SESSION_ID` route-to-session path (v4, same one `expand()` uses); no session →
`EXTRA_HANDOFF_NEW_CHAT` (the app's default landing IS new chat). A typed draft rides
`EXTRA_HANDOFF_DRAFT` → chat destination `SavedStateHandle` (`HANDOFF_DRAFT_KEY`, raw value — NOT a
route arg, arbitrary text isn't URL-safe) → `ChatViewModel.seedHandoffDraft` → one-shot apply in
`ChatSurface` that deliberately does NOT tag the pending send Voice (a masqueraded Voice tag would
trigger speak-along on a typed draft's reply — its own seam, not a ride on `DictationState.Final`).
The drag offset carries an Idle-keyed `snapTo(0f)` reset per the draft-clear law above.

## Companion chip slot + pill drag-handle hint [R4]

The overlay presence redesign added two small chrome pieces on top of Task 1's 84dp/56dp
pill mass, both additive — no chip visibility condition or pull-up mechanics changed:

- **Drag-handle notch.** `AuraComposer`'s new `showDragHandle: Boolean = false` param (default off,
  so the docked in-app composer — its only other caller — stays byte-identical) renders a small
  top-center notch when `showDragHandle && !expanded`: hidden while the pill is expanded (chips/
  multiline draft), since a grown pill has no stable top-center resting spot for it. It's a visual
  AFFORDANCE HINT for the pull-up gesture above, not a second gesture surface of its own — the whole
  pill remains the drag target (`FloatingComposerBar`'s own `pointerInput`, unchanged). It
  deliberately reuses `AuraSpacing.ResponseCard.dragHandleWidth`/`dragHandleHeight`/
  `dragHandleTopOffset` VERBATIM (the exact tokens `ResponseCardDragHandle` already draws) rather
  than minting a second handle size — one handle vocabulary in this file, not two. Only
  `FloatingComposerBar`'s `AuraComposer(...)` call passes `showDragHandle = true`.
- **`CompanionChipSlot`** (private, `AssistOverlayScreen.kt`) is the ONE named slot for
  contextual chips above the pill — today the inline `ErrorCard` (client-side
  `AssistUiState.Error`, retry-wired) and `ContinueLastSessionChip` (`AssistUiState.Ready` with a
  `lastSessionTitle`), nothing else (YAGNI — don't add a third chip kind without a real product
  need). It replaces what were two inline `if` blocks in the bottom `Column` with the exact same
  conditions/positions/callbacks — a pure extract, not a behavior change. Both chips now share one
  fade+rise entrance (`fadeIn(AuraMotion.composerMorphSpring) + slideInVertically { it / 2 }`) so
  they arrive as part of the overlay's choreography instead of popping in. Each chip's content
  lambda re-derives its own typed state (`state as? AssistUiState.Error ?: return@AnimatedVisibility`,
  `state as? AssistUiState.Ready`) rather than trusting the outer `visible` condition — `AnimatedVisibility`
  keeps rendering its content during the EXIT transition off a snapshot, so a state flip mid-exit
  (e.g. Error → Ready before the fade finishes) must never crash on a stale cast; this is the same
  defensive shape `ComposerActionCircle`'s exhaustive-but-unreachable `Idle` branch already uses for
  an identical mid-crossfade-race reason.

Verification note: the client-side `AssistUiState.Error` path (`ErrorCard`) is reachable on-device
via `AssistOverlayPreviewActivity` by pointing `SettingsStore`'s base URL at an unreachable host
(`-e seedBaseUrl http://127.0.0.1:1 -e mockBackend false` on `.MainActivity`, then send any query) —
this fails `AssistTurnMachine.beginTurn`'s `createSession`/`sendQuery` call directly, landing on
`AssistUiState.Error` before any stream starts. The mock backend's own `"error"`-keyword scenario
(`MockScenarios.errorScenario`) does NOT exercise this chip — it scripts a `completion`-level
`done_reason: "error"` that the reducer folds into an in-transcript error `ChatItem` (rendered
inside the `ResponseCard`'s `ChatTranscript`, Retry link and all), never transitioning
`AssistUiState` to `Error` at all; don't confuse the two failure surfaces when writing a repro.

## Dismiss choreography: glow extinguishes WITH the pill + contained ✕ [R4]

Dismiss polish, two independent fixes in `AssistOverlayScreen.kt`:

- **Shared exit-window constant.** Before this fix, the glow's dismiss used `AuroraEdgeGlow`'s quick
  default (`EdgeGlowUniformMath.VISIBILITY_TRANSITION_MS`) while the composer pill's own manual exit
  `Animatable`s (in `FloatingComposerBar`'s `LaunchedEffect(visible, reducedMotion)`) used a locally
  computed `((barSlideSettleMs - barSlideStartMs) / dismissSpeedMultiplier)` tween — two independent
  formulas that happened to read the same `AuraMotion` tokens but could drift apart the moment either
  one changed. `OVERLAY_GLOW_DISMISS_MS` (top-level private val, ≈169ms) is now the ONE constant both
  read: passed as `AuroraEdgeGlow(..., dismissFadeMs = OVERLAY_GLOW_DISMISS_MS)` and as the `tween()`
  duration for both pill-exit `Animatable`s, replacing the old locally-scoped `exitMs` val. This makes
  "glow and pill extinguish together" a structural guarantee (one shared number), not a coincidence of
  two formulas matching. Uses `AuroraEdgeGlow`'s existing hold-frame dismiss fade (see
  `ui/aurora/CLAUDE.md`'s "Run-end ease-off" — same mechanism `ChatScreen` uses for its own longer
  ≥3s photosensitivity fade, just a much shorter window here since the overlay's dismiss isn't a
  flash-risk-relevant transition) — never a uniform snap (§7.15).
- **Device-verified, real screencap.** redroid's screencap round-trip here is ~1-1.2s (far slower
  than the 169ms window itself), so a frame-accurate burst capture isn't possible on this device —
  but a genuine mid-dismiss frame WAS caught: the composer pill (mic circle) still on screen
  alongside a faint, center-weighted blue bottom-edge tint (pixel-sampled, confirming the glow was
  still fading, not already snapped to black), followed by a fully clean home screen with zero
  residual glow in the very next capture. No frame ever showed the glow alone after the pill had
  already vanished (or vice versa) — consistent with "no ghost," within this device's capture
  resolution. The AssistOverlayPreviewActivity host's `onDismiss = { machine.dismiss(); finish() }`
  calls `finish()` synchronously right after the state flip, so the whole exit choreography here (not
  just the glow) races against Activity teardown on this host, same as it always has — a pre-existing
  host characteristic, not something this fix introduced or could route around without adding a
  test-only delay to the preview activity itself.

**Contained dismiss ✕ (chrome containerization).** `OverlayChrome`'s `IconButton` was the ONLY
floating element at the top of the overlay with no container of its own, AND its `Icon` was the ONLY
overlay icon with no explicit `tint` — it inherited `LocalContentColor` (`Color.Black` under
`MaterialTheme`) over the dark scrim, rendering as fully invisible pixels in both hosts (`OverlayChrome`
mounts unconditionally in both `AssistOverlayScreen`'s real tree and `AssistOverlayPreviewActivity`'s
direct composition — there is no separate reduced preview tree here). Fix: the same
`surfaceIconScrim`-disc treatment the chat top bar already uses for icons over content —
`Modifier.background(color = AuraColors.surfaceIconScrim, shape = AuraShape.radiusPill)` on the
`IconButton`, plus `tint = AuraColors.iconPrimary` on the `Icon` (never rely on the inherited default
again). Screenshot-confirmed: previously all-black pixels at the top-left, now a visible dark disc
with a legible glyph.

## Outside-tap dismiss + the pointer-layering law

The overlay window is modal (`TYPE_VOICE_INTERACTION` — receives touch, no pass-through) and the scrim
(`OverlayScrim`) is DECORATIVE, consuming nothing. Before the only pointer handler over the scrim
was the swipe catcher (`detectVerticalDragGestures` only), so a plain TAP on empty scrim simply died —
and with nothing dismissing, the ✕'s smooth exit choreography never played. Dismiss is now THREE
affordances routed through the ONE `callbacks.onDismiss` (→ `AuraSession.dismissSession()` →
`machine.dismiss()` + `hide()`, so draft-clear / TTS-stop / haptics AND the `state → Idle` exit
choreography — glow extinguishing WITH the pill over `OVERLAY_GLOW_DISMISS_MS` — all ride the ONE proven
teardown path; reduced motion needs nothing extra, that exit already snaps under `extras.reducedMotion`):

1. the contained **✕** (`OverlayChrome`);
2. the scoped **swipe catcher**, which now ALSO handles a plain tap — a second `detectTapGestures`
   alongside its existing drag detector (two `pointerInput`s on one node compose fine: the tap fires on
   a stationary press, the drag on movement past slop). Still scoped ABOVE the card's reserved region
   (`1f - maxHeightFraction`) so its DRAG never competes with the card's `ChatTranscript` scroll
   (don't reintroduce a whole-screen drag catcher);
3. a full-screen **gap layer** LOW in the z-stack (above the scrim, below all content) carrying
   `detectTapGestures { onDismiss() }` plus the single `semantics { onClick("Dismiss assistant") }`.

Any new non-`Surface` overlay content over the scrim must ABSORB its own taps or the outside-tap fires
under it: `ResponseCard` (a bare `Column`, not a `Surface`) opts in via `detectTapGestures {}`, DOWN-only,
so the drag handle's swipe-up and the transcript's scroll are untouched.

**Pointer-layering law (verbatim, fix-overlay).** Compose hit-testing routes a pointer event ONLY
to the topmost hit sibling chain at that position — a lower overlapping sibling is NOT in the hit path,
so consumption is irrelevant; a single under-everything dismiss layer is insufficient. EVERY
pointer-handling layer over the scrim must itself route plain taps to `onDismiss` (this is why the swipe
catcher gained a tap detector — a tap in its region previously died in the drag detector and never
reached the gap layer beneath, device-verified). The gap layer only catches taps where no pointer-input
sibling overlaps it: the card's side margins, and the below-catcher / above-card band. Pure-draw layers
(`AuroraEdgeGlow`) have no `pointerInput` and are hit-test-transparent, so a tap passes THROUGH them to
the gap layer beneath. The single `semantics onClick("Dismiss assistant")` lives on the gap layer and is
TalkBack-reachable independent of pointer z-order — an accessibility action is dispatched through the
SEMANTICS tree, not pointer hit-testing (label-only, no `contentDescription`, so it contributes a
dismiss action without shadowing the composer/card as a giant labelled region or stealing focus; the ✕
keeps its own independent label).

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

## Screen-reader state announcer ([R4 2026-07-10])

`OverlayStateAnnouncer` (private, `AssistOverlayScreen.kt`) is the overlay's non-visual state
channel — a zero-sized (`1.dp`) `Box` with `semantics { liveRegion = LiveRegionMode.Polite;
contentDescription = ... }`, mounted as the FIRST child of `OverlayChrome`'s `Box` so it's always
present regardless of which other chrome is showing. TalkBack announces the `contentDescription`
whenever it changes.

**The string is keyed on state-KIND, never on a state's payload fields** — this is the whole
point: `Listening(partial, rmsDb)` maps to the same fixed string ("Mewbo Assistant, listening")
regardless of the live `rmsDb`/`partial` deltas that tick many times a second, and `Streaming(items,
done, speaking)` maps on `done` alone (constant "Responding" through every delta-driven `items`
growth, flipping to "Response ready" exactly once). Keying on payload instead of kind would spam
TalkBack on every waveform tick or streamed token — the orb/glow/waveform are already the
high-frequency visual channel; the announcer's whole value is being a LOW-frequency non-visual one.
`Ready`/`Sending` map to fixed strings too (payload-independent by construction — `Ready` even
ignores `lastSessionTitle`). `Idle` announces nothing (`null` short-circuits before the `Box` is
even emitted) since the overlay is never actually on screen in that state (`AssistUiState.Idle`'s
own KDoc).

**Dismissal is deliberately unannounced.** There is no "dismissed"/"closed" string anywhere in the
`when` — the window teardown (`onDismiss` → `machine.dismiss()` → host `finish()`/`hide()`) races
TalkBack's own announcement queue, and focus returning to the host app IS the dismissal signal a
screen-reader user already gets for free from the platform. Adding a competing announcement here
would either lose the race silently or double-announce over the platform's own focus-return cue.
TalkBack itself (does an actual screen reader receive and speak these announcements correctly) is a
physical-Pixel human verification item — redroid/the preview activity confirm the states still
render and the announcer never crashes, nothing about a real TalkBack session.

## Real-session-vs-preview geometry note

The real `VoiceInteractionSession` window's on-screen geometry differs from
`AssistOverlayPreviewActivity`'s (the debug fast-loop host) — the composer has been measured sitting
~1000px lower in device coordinates on the real session window than in the preview activity. Before
`decorFitsSystemWindows(false)` was set explicitly (`voice/CLAUDE.md` — the soft-input mode itself is
left at the platform pan default, never `ADJUST_RESIZE`/`setSoftInputMode`; see § "IME: the platform
pan is the ONLY keyboard mechanism"), the two hosts had DIFFERENT unconfigured platform soft-input
defaults, which is the leading explanation for that offset — now that both request the same explicit
`decorFitsSystemWindows` regime, re-verify this gap on a real device before assuming it's fully
closed. When driving the real session via `adb input tap`, always measure
coordinates from an actual screenshot of the SESSION window, never reuse preview-activity
coordinates (`voice/CLAUDE.md`'s own verification-loop guidance).
