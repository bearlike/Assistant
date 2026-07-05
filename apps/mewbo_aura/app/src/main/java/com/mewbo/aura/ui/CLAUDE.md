> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md) · children: [aurora](aurora/CLAUDE.md) · [overlay](overlay/CLAUDE.md) · [composer](composer/CLAUDE.md)

# Aura UI — Compose Surface Guidance

> **Canonical design system: [`DESIGN.md`](../../../../../../../../DESIGN.md).** Token VALUES,
> visual laws, provenance, and the "never again" regression registry live there — one place, not
> restated across the scoped CLAUDE.mds. When a number or a law below disagrees with DESIGN.md,
> DESIGN.md wins and this file is stale — fix it. Where this doc still cites a value it is either
> load-bearing for a mechanism it is explaining or a pointer to the canonical section
> (typography → DESIGN.md §2, space/rhythm → §3, color/shape → §4, state/liveness → §5).

Scope: `ui/` — theme, orb, screens, shared chat components. Spec: Gitea #175. Decision-heavy
subtrees now have their own CLAUDE.md (children above) — read the child before re-deriving anything
about shaders, the assist overlay, or the composer; this file stays the thin router + whatever's
genuinely shared across all of `ui/`.

## Theme stack (single source of visual truth)

`MaterialTheme` — NOT `MaterialExpressiveTheme`/`MotionScheme`, both
`internal` in m3 1.4.0 and unreachable (`javap` misleadingly shows them
public; Kotlin visibility is frontend-enforced) — wraps a static, dark-only
`ColorScheme` → M3 `Shapes` from `AuraShape`'s radius tokens → `AuraType`'s
4-style `Typography` on one Figtree variable font → `AuraMotion`'s named
M1–M8 springs as the ONLY spring source → `AssistantExtras` CompositionLocal
(orb palette per state, scrim color, reduced-motion flag — `glassSheet`/
`GlassSheetStyle` DELETED with W3's Haze removal; no glass/blur anywhere,
spec §2) → AGSL orb consuming extras.

`AuraSpacing`'s dp values carry provenance in their KDoc (Rev C eyeballed
estimate vs Rev F "measured" vs Gitea #181 real-device-capture measured — an instrumented
uiautomator/pixel-ink audit, or four-capture pixel-sampling, against the real reference app).
Governance when sources conflict is the DESIGN.md §1 precedence ladder — **the newest user
directive wins over every measured reference value**: 2026-07-04 directives > #181's real-device
captures > the six reference-capture frames (F1–F6) > Rev F live-rig measurements > Rev C eyeballed
estimates. Never chase live-app drift a higher-precedence source didn't show, and never "fix" a
directive value back toward reference parity — record a new directive instead. There were TWO
2026-07-04 passes: a **compact-scale** pass (shrank the whole type ramp one step) then a
**breathability round 2** pass (nudged line-heights up + opened the inter-turn/bubble→reply/footer
gaps). Both moved the same tokens; **the settled values are canonical in DESIGN.md §2 (type) / §3
(space)** — do not re-enumerate them here. The two structural mechanisms those passes introduced
that still matter at this layer: two previously-implicit ink-from-leading gaps became explicit
tokens (`AssistantText.topGapAfterBubble` and `ActionRow.topMargin` — no longer 0dp leaning on
font leading, §7.10 regression), `ActionRow.iconSize` shrank inside the unchanged 48dp a11y cells,
and `AuraSpacing.Turn { gapAbove/gapBelow }` was added for the turn divider (below).

**Token discipline (enforced in review):** no `Color(`, dp radius
literals, no `spring(` outside `ui/theme/`. Need a new color/shape/motion?
Add a token to the theme, then consume it. The web console learned this the
hard way — literals ship looking right and rot the first palette change.

## Orb (`ui/orb/`) — the brand clay-flower SDF (v2)

The orb IS the brand mark: an AGSL **polar SDF of the console's 8-lobed
clay flower** (`BrandMark.tsx`, viewBox 480), with r(θ) coefficients
FFT-fit from the real SVG path — not a generic radial-gradient blob. v1
shipped soft gaussian blobs and was rejected by the user as "blurry, low
quality"; don't regress toward decorative blur.

Non-negotiables encoded by v1's review round:
- **Crisp edges**: silhouette anti-aliased with ~1.5px smoothstep windows;
  interior effects (iridescent sweep, shimmer) masked INSIDE the silhouette.
- **Halo/ring terms carry an explicit edge window**
  (`1 - smoothstep(0.44, 0.5, dist)`) so alpha is exactly 0 before the draw
  bounds — falloff constants alone shipped a visible square edge once.
- **ALL per-state uniform derivation lives in `OrbUniformMath`'s exhaustive
  `when(state)` functions** (incl. `pulseEnvelope`); a new `OrbState` must
  fail compilation, never silently lose behavior. No `if (state is X)` at
  call sites; transition-driven scale via `graphicsLayer{}`, never
  `Modifier.scale()`.
- Frame loop pauses when not STARTED (lifecycle-gated); uniforms update in
  the draw phase only — animation never recomposes `Orb()`.
- Reduced-motion reuses the SAME crisp shader frozen (+ opacity pulse).
- Visual verification host: debug-only `LivenessShowcaseActivity` (page
  gallery incl. the orb states; superseded the deleted `OrbShowcaseActivity`
  once `ui/aurora/` shipped, its own "Bottom glow" page later retired with `AuroraGlowBottom`'s
  deletion — Gitea #181). redroid is SwiftShader: judge
  geometry/crispness there, never FPS.

`ui/orb/GlslNoise.kt` (hash/value-noise) and `ClayFlowerSdf.kt` (the
`petalShape`/`flowerShapeAlpha` polar SDF) are shared AGSL fragments
extracted from `Orb.kt`; `ui/aurora/AuroraEdgeGlow.kt` and the brand mark
(`AuraSpark`, `ui/orb/AuraSpark.kt`) both consume them rather than
re-deriving the flower geometry. `ShaderFrameClock.kt`
(`rememberShaderTimeSeconds()`, `ShaderRotationAccumulator`) is the shared
frame-clock/rotation-integrator all three shaders use.

**Shader-family rules (falloff shape, dither, hue-blend method, tuning-constant provenance, the two
time-based traps, verification host) moved to [`ui/aurora/CLAUDE.md`](aurora/CLAUDE.md)** — that
file now owns the `AuroraEdgeGlow`/`AuroraWashTop` rebuild's postmortems in full; `Orb.kt`'s own
`OrbUniformMath` error-pulse envelope still carries the identical latent
`LaunchedEffect`-ordering-race pattern documented there (low-visibility, still unfixed) — read that
file before touching either shader.

## Compose stability — measured, not claimed

`@Immutable` on `ChatItem`/`ChatUiState` is load-bearing: a single
`List<...>` field makes the whole sealed hierarchy Compose-unstable and
every visible row re-executes per streaming delta. When touching item
models or row composables, re-verify with the counter method from task-E
fix round 1: temporary Log-in-composition counters + a scripted delta
sequence in `ChatPreviewActivity` → streaming row N/N, other rows 0/N.
Numbers, not narrative, before claiming recomposition discipline.

Two real breaks found this way, both silent (compiled fine, just recomposed
too much): a per-item callback CURRIED at the list-dispatch site (a fresh
`{ onX(item) }` built inside the `itemsIndexed`-style dispatch function)
breaks the row's own parameter stability and forces it to recompose on ANY
sibling's change, not just its own — curry only inside the row's own body,
pass the raw `(Item) -> Unit` down. A callback-BUNDLE (e.g. `ChatCallbacks`)
constructed inline on every recomposition of the host composable defeats
stability no matter how far downstream you curry — hoist it behind
`remember(viewModel, ...) { ... }`.

Chip-arrival tracking (`ChatTranscript`'s `ChipEntranceAnimator`, see Chat
rendering rules) uses a plain `remember { mutableSetOf<String>() }` for
"seen keys," deliberately NOT `mutableStateOf`-backed — reading a shared
`State`-backed set from every item's composition would recompose EVERY chip
whenever any one new key got added to it.

## Chat rendering rules

- `LazyColumn(reverseLayout = true)`; stick-to-bottom unless user scrolled
  up (then "↓ New responses" pill). Items come ONLY from
  `TranscriptReducer` output — never hand-assemble transcript rows.
- Assistant text — streaming AND finalized — renders ONE path:
  `MarkdownMessage(MarkdownBuffer.sanitize(rememberStreamedText(text, isStreaming)))` (mikepenz
  `Markdown`, m3). `WordFadeText` is DELETED (user directive 2026-07-04: show real markdown as it
  streams, no word-fade comet-tail; also retired the earlier `StreamingText`).
  Two mechanisms keep the stream from flickering, both mandatory (DESIGN.md §6 / [R16]):
  `MarkdownMessage` parses through a `retainState = true` `rememberMarkdownState` — the default
  resets to a blank `State.Loading` on every content change, so per-delta that blanked the whole
  reply for a frame before the async reparse landed; `retainState` keeps the last parsed tree
  visible until the new parse swaps in. `rememberStreamedText` throttles the reparse to
  `AuraMotion.markdownStreamReparseMs` (~20 Hz) while streaming and passes finalized text straight
  through, so the growing tree isn't relaid-out every token. Do NOT add a per-word reveal animation
  to "smooth" streaming — that path was retired; smoothness is the absence of churn.
  The `MarkdownBuffer.sanitize` guard is what makes mid-stream rendering safe —
  it closes half-open fences/brackets so an unclosed markdown token can't
  explode layout, and equally covers a finalize event arriving after streaming
  already closed. `ChatItem.AssistantMessage.isStreaming` still flows through for
  the reducer/`ChatTranscript` liveness facts; it just no longer selects between
  two renderers.
- Recomposition scope: ONLY the in-flight message row recomposes on delta;
  the list and finalized rows must not (stable keys = event ids, lambdas
  hoisted — see Compose stability above for two ways this silently broke).
- Tool results fold into ONE `ToolCallGroup` per turn (v3/#177; turn-scoped
  grouping + insert-before-narration ordering now live in the reducer — see
  `data/CLAUDE.md` turn invariants, not here). `ToolCallGroupCard` is CARDLESS
  (restyled 2026-07-04): a plain row, no filled `Surface`/card — reference-style,
  hierarchy via the 24dp gutter indent + hairlines, never nested cards. It is
  default-COLLAPSED even during a live run (user tap always overrides;
  auto-expanding on every run was the "why is this open every time" complaint).
  The header ALWAYS shows the count — `Using N tools…` (pulsing via the shared
  `TypingIndicator`) while the run is live, `Used N tools` once settled. Expanded:
  a hairline under the header and BETWEEN consecutive tool rows; per-call summary
  hidden while collapsed (`textTertiary` caption when expanded); input AND result
  share ONE monospace `ToolCallCodeBlock` (`surfaceSelected`) — never a sheet,
  never raw JSON outside that on-demand block. Chip-family items are gutter-inset
  (24dp), 16dp below a user bubble (`AuraSpacing.ActivityGroup`). `ChatItem.TodoList`
  → `PlanCard`, sub-agent → `AgentChip`, both also gutter-inset. M7 entrance on
  first arrival only; M8 = flat fade.
- Pickers ARE `ModalBottomSheet`s (model picker, composer options —
  reference-parity); only the chip/tool DETAIL sheet stays dead (in-place
  expansion). Sheet anatomy: `surfaceInput` container, `radiusBubble` top
  corners, drag handle, 56dp rows.
- The docked chat tree applies `imePadding()` at its root (`ChatSurface`)
  — the activity is edge-to-edge, so `adjustResize` alone does NOTHING in
  Compose and the keyboard fully occludes the composer without it.
- Item spacing is asymmetric by design, not a uniform `Arrangement.spacedBy`
  — each item's top padding depends on the chronologically-preceding item's
  type (`AssistantText.topGapAfterBubble` — now an explicit token, no longer a
  0dp leaning on font leading — after a `UserBubble`; `ActivityGroup.topGapAfterBubble`
  when a chip-family item follows a bubble; `paragraphGap` otherwise; values in DESIGN.md §3).
- **Turn separation is BORDER + space.** At every turn boundary — a `UserBubble`
  with a chronological predecessor — `ChatTranscript` draws a
  `HorizontalDivider(outlineHairline)` padded by `AuraSpacing.Turn.gapAbove`/
  `gapBelow` (the divider sits in a `gapAbove` + hairline + `gapBelow` band; values in
  DESIGN.md §3); this REPLACES that item's normal top gap, never stacks with it. The reducer's
  activity-precedes-narration guarantee (`data/CLAUDE.md`) is what lets each boundary cleanly
  bracket one whole turn against the last. The "Mewbo is an AI tool and can make mistakes." disclaimer is a
  transcript-level SIBLING (not a property of `AssistantMessageRow`) declared as its OWN keyed
  `"disclaimer"` item at the LazyColumn's DSL position 0 — under `reverseLayout` that renders it at
  the visual BOTTOM, at the very END of the whole transcript, below the newest response and its
  footer, and it stays pinned there as new turns append. Gated on ANY completed assistant message
  existing (`hasSettledReply`), so never during the first turn's stream. It is
  NEVER first-turn-anchored and NEVER mid-transcript — both of those shipped and were reverted
  (DESIGN.md §7.7).
- **Run liveness — solid at rest, one glow layer, one spark** (user directive 2026-07-04, "always
  tell running from dead"; DESIGN.md §5). The app's resting state is a SOLID background with NO
  aura and a transparent top bar — an always-on chat wash shipped and was reverted the same day
  (DESIGN.md §7.2/§7.3), so there is no resting/top wash of any kind in chat. Two live cues remain:
  (1) `ChatScreen`'s bottom blue `AuroraEdgeGlow` (`IN_APP_GLOW_*` user-tuned reach/center-weight/
  alpha) is the SOLE liveness layer — `EdgeGlowState.Thinking` while `Sending`/`Streaming` (with
  `IN_APP_GLOW_ACTIVE_SPEED_SCALE` for a livelier drift), and an ambient `Listening(0f)` breathe
  ONLY inside the fresh-invocation window (`AMBIENT_INVOCATION_WINDOW_MS`), after which it fades to
  `Hidden` and rest is solid again; (2) the `AuraSpark` row at the transcript bottom is the
  PERSISTENT cue, shown for the ENTIRE `Sending`/`Streaming` phase (`showThinking = isRunLive`),
  covering follow-up sends and mid-turn tool phases, not just the pre-first-delta gap. Ownership
  law: `ChatScreen` is the ONE glow owner; `ChatSurface` renders NO aura of its own (it only
  derives `washActive` to style `ChatTranscript`'s `overWash`). `AuroraWashTop` is
  showcase-only now. Shader math + the solid-resting/speedScale/top-fade laws live in
  [`ui/aurora/CLAUDE.md`](aurora/CLAUDE.md).
- **One chat tree, reused as a COMPONENT, not forked — since v5/#181, the assist overlay renders
  chat again**: `ChatTranscript` is the ONE shared composable both `ChatSurface` (docked, `MainActivity`
  host) and the overlay's `ResponseCard` (`ui/overlay/CLAUDE.md`) render, never a second rendering
  path. `ChatTranscript` gained one additive parameter for this, `fillParent: Boolean = true` (Gitea
  #181 P3) — default preserves the exact prior in-app behavior byte-for-byte (`ChatSurface` never
  passes it); `false` (the overlay's ONE caller) lets the internal `LazyColumn` shrink-wrap its own
  content within a `heightIn(max = ...)` cap instead of always forcing a full-size slab. Extend this
  parameter surface for any future overlay-vs-docked sizing difference — never fork the composable
  itself to get one.
- Composer input is multi-line (maxLines 6, then internal scroll): wrapping
  drives the SAME growth branch as staged-attachment chips
  (stadium→radiusBubble + fixed-height drop), calibrated to the reference
  app's field (26dp/line upward growth, bottom edge pinned).
- Pre-session scope indicator (`ComposerScopeIndicator`, ChatSurface) shows
  `project · N tools` above the composer only while no session exists; tap
  opens the composer options sheet. Default project seeds from
  `SettingsStore.selectedProject`; a per-chat override wins until the next
  new chat.

## Screens

`navigation/` drawer (`AuraDrawerContent`: 78% rail, current-location pill) replaced the old
`sessions/` list screen; `sessions/` now holds view-state/viewmodel plumbing **plus** the two pure,
unit-tested rail helpers `RecentsFilter` (mobile-only ⇄ all scope predicate) and `SessionGrouping`
(Today / Previous 7 days / Older buckets). The Recents section (2026-07-03 directive, DESIGN.md §3
Recents-rail law): a "Recents" header carries an overflow **scope filter** (default
`RecentsFilter.MOBILE_ONLY` → only `origin == "mobile"` sessions, mirroring the console's
`DEFAULT_VISIBLE_ORIGINS`; "All" shows every session), the ViewModel holds the filter, and the
drawer client-side filters + groups the fetched list (`GET /api/sessions` returns everything, so no
starvation). Rows are a dedicated **compact** composable — title flush at `screenGutter` (NOT the
icon-slot `DrawerRow`, whose empty leading box indented titles ~36dp; DESIGN.md §7.13), with the
`running` liveness dot moved TRAILING. `DrawerRow` still backs the icon-bearing New chat / Search
chats action rows. Long-pressing a recents row opens `SessionActionsSheet` (Rename / Archive over
`SessionsViewModel.renameSession`/`archiveSession`; no Delete — the API has no hard-delete). Its
laws live in DESIGN.md §3 "Recents-row long-press actions"; the two device-verified IME rules
(auto-focus the rename field with full selection; hide the keyboard eagerly on commit AND
on-dispose) must not be simplified away — each shipped broken once without them. Debug-only: the mock backend (`mock/MockSessionStore.seedDemoIfEmpty`) seeds a
mixed-origin, date-spread demo list so the grouped rail + scope filter are on-device testable with
no live backend. `chat/` is always the
landing destination (no separate empty landing screen; its SOLE liveness layer is a bottom
`AuroraEdgeGlow` bloom owned by `ChatScreen` — the shared overlay shader reused with `IN_APP_GLOW_*`
tuning, rendered behind a TRANSPARENT `Scaffold` so canvas + glow run edge-to-edge under the header
and system bars. Rest is a SOLID background with NO aura and a transparent top bar: the glow
ambient-breathes (`EdgeGlowState.Listening(0f)`) only for `AMBIENT_INVOCATION_WINDOW_MS` on a fresh
invocation, then goes `Hidden`; it returns as `EdgeGlowState.Thinking` only while a run is
`Sending`/`Streaming`. There is NO top wash — a chat wash + the old decorative `AuroraGlowBottom`
disc were both deleted; an always-on wash briefly shipped and was reverted, DESIGN.md §7.2) ·
`search/` (search chats) · `settings/` (base URL, masked API key, default project, default-assistant
shortcut, display name, reduced motion) · [`overlay/`](overlay/CLAUDE.md) (scrim →
`AuroraEdgeGlow` → the shared D-1 orb dock + floating composer pill +, since v5/#181, a
`ResponseCard` for the first turn's streaming response — see that child CLAUDE.md for the full
contract; owned by the voice session host).
Empty/error/offline states are first-class — no blank screens.

## Accessibility

TalkBack labels for orb states; touch targets ≥ 48dp; reduced-motion
honored everywhere motion exists.
