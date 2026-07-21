> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Composer — ui/composer/

Scope: `ui/composer/` — `AuraComposer` (one component, five `ComposerState`s, consumed by both the
docked in-app chat and the assist overlay), `ComposerState`/`ComposerStyle`, `RmsWaveform`,
`ComposerOptionsSheet`. Spec §6.2 (C1-C5), Rev E §E-3 frame corrections (overlay
margin correction).

## Two pill widths/heights/type scales/circle sizes, never share one token

`ComposerStyle.Docked` (in-app chat) and `ComposerStyle.FloatingOverlay` (assist overlay) render
the SAME five `ComposerState`s and anatomy, but every geometry axis that distinguishes them is a
DIFFERENT, independently measured/directed token — never one token consumed with a
style-conditional multiplier:

- `AuraSpacing.Composer.horizontalMargin` = **16dp** — docked, in-app.
- `AuraSpacing.Composer.overlayHorizontalMargin` = **38dp** — floating overlay pill
  (pixel-sampled from real-device captures; the original spec estimate of 16dp for the
  overlay was wrong).
- `AuraSpacing.Composer.height` (docked) vs. `AuraSpacing.Composer.overlayHeight` (overlay) [R4
  2026-07-10] — values in DESIGN.md §3; the overlay pill reads as a full conversational surface,
  "large FAB-and-a-half."
- `AuraSpacing.Composer.actionCircleSize` (docked) vs. `AuraSpacing.Composer.overlayActionCircleSize`
  (overlay) [R4 2026-07-10] — values in DESIGN.md §3; the primary voice action dominates the overlay
  pill's trailing side; threaded via `ComposerTrailingCluster`'s `size` param to
  `ComposerOverlayMicCircle`/`ComposerActionCircle`/`ComposerVoiceModeTile`/`ComposerStopTile`, all
  four of which default to the docked size so every OTHER caller stays byte-identical.
- `AuraType.bodyMessage` (docked) vs. `AuraType.composerOverlay` (overlay) [R4 2026-07-10] — size/
  line-height in DESIGN.md §2; the field/invitation type scale, so the invitation reads comfortably
  title-scale in the overlay pill.

These are deliberately SEPARATE `AuraSpacing`/`AuraType` fields per axis, not shared tokens
consumed with a style-conditional multiplier — a future in-app change on any of these must never
accidentally drag the overlay pill's geometry along with it, or vice versa. `ComposerStyle.pillColor`
(Docked → `surfaceInput`, FloatingOverlay → `surfaceOverlayPill`) was the original precedent for
"same component, two independently-tokened style axes"; `pillHeight`/`actionCircleSize`/
`fieldTextStyle` (private `ComposerStyle` extensions next to `pillColor` in `AuraComposer.kt`)
follow the identical shape.

## Docked scope-row alignment anchor [2026-07-14]

The docked composer's pre-session scope row (rendered by `ChatSurface`, [`ui/chat/CLAUDE.md`](../chat/CLAUDE.md)
owns its content + faceted counts) aligns its leading glyph to the pill's straight-edge start via the
new token `AuraSpacing.Composer.scopeRowStartInset = horizontalMargin + height/2` (16 + 32 = 48dp).
`radiusPill` is `CircleShape` — a 50% stadium — so the corner-radius end sits exactly `height/2` in
from the pill edge: that's the "where the curve becomes the straight line" anchor, reusable for ANY
composer-content alignment (values → DESIGN.md §3). Do not eyeball a scope-row indent against
`horizontalMargin` alone — it would sit under the stadium curve, not the straight edge.

## Repeating section headers: top-heavy asymmetric rhythm

`ComposerOptionsSheet`'s `ScopeSectionHeader` (the provenance dividers layered over the per-server tool
groups) pads **top-heavy and asymmetric** — full `AuraSpacing.DrawerRow.sectionHeaderTopPad` above, NO
explicit gap below — so a boundary clings to the content that FOLLOWS it (~2:1, the same "headings cling
to what follows" shape as `AuraSpacing.Markdown.headingTopGap`, matching `RecentsHeader` /
`SettingsScreen`). The **symmetric `sectionHeaderTopPad / 2` top-AND-bottom form is ONLY for a single,
non-repeating header** (`ActionSheet.SheetHeader`, `ModelPickerSheet`): in a REPEATING list it gave
every provenance boundary the same visual weight as an ordinary intra-section row gap, so the boundaries
read as unclear. `ServerGroupHeader`/`ToolRow` are bare, zero-gap fixed-height rows, so this
header's own top/bottom asymmetry is what carries the entire boundary signal — don't restore symmetry here.

## `DictationCenterContent`'s `partialText` null-vs-blank contract

`ComposerState.Dictation.partialText: String?` is the center content's ONLY signal for "show the RMS
waveform bars" (`null`) vs. "show the finalizing transcript text" (non-null string, spec §6.2:
"transcript may replace bars as words finalize"). Every state-producing call site MUST pass
`listening?.partial?.takeIf { it.isNotBlank() }`, never the raw `listening?.partial` value —
`AssistUiState.Listening.partial` (and `DictationState.Listening.partial`, the in-chat composer's
equivalent) defaults to `""` the INSTANT dictation starts, not `null`. Passing the raw empty string
makes `partialText != null` permanently true from the first frame, so the RMS bars branch of
`DictationCenterContent` never renders at all — the C3 waveform is dead the moment dictation starts,
with no visible error, just silently wrong center content (a real regression fixed twice at two
different call sites: the assist overlay's `FloatingComposerBar` and the in-app composer's own
binding — grep both before touching either). Any THIRD caller that resolves a `ComposerState.Dictation`
from a raw partial-transcript string must apply the same `takeIf { it.isNotBlank() }` — this is not
optional cosmetic cleanup, it's the only thing that keeps the null-means-"nothing transcribed yet"
contract this composable actually depends on.

## C3 fluid voice bars (`RmsWaveform`, [R5 2026-07-11])

The 28-bar RMS meter is a per-bar simulation, not one envelope scaling a fixed taper shape — a
single `animateFloatAsState` envelope shipped in 0.0.30-debug and read as "a triangle moving," per
direct user device feedback. `RmsWaveformMath.barTarget(index, rms, timeSeconds)` computes a
FLOWING pseudo-spectrum target per bar (only RMS exists — no FFT: a `valueNoise1d` field sampled at
`index * PER_BAR_PHASE + timeSeconds * FLOW_HZ`, tapered by the pre-existing `shape` center-weight),
and `stepBars` drives each bar toward its own target with a critically-damped spring
(`SPRING_HZ`) plus neighbor coupling (`COUPLING_PER_SECOND`, a discrete-Laplacian diffusion term) so
energy visibly travels across the row frame to frame. `heightFraction`/`envelopeSpec` and
`AuraMotion.rmsDecayMs` are DELETED (the spring replaces the tween-based envelope entirely — grep
before reviving either token, no remaining consumer as of this rewrite). Rendering is ONE `Canvas`
(`RmsWaveform`), stepped by a `withFrameNanos` loop that mutates plain `heights`/`velocities`
`FloatArray`s and bumps a `frameTick` read ONLY inside the `Canvas` draw lambda — zero per-frame
recomposition (house draw-phase law, `ui/CLAUDE.md`). All constants (`FLOW_HZ`, `PER_BAR_PHASE`,
`SPRING_HZ`, `COUPLING_PER_SECOND`, `NOISE_FLOOR`, `MAX_STEP_SECONDS`, `SUB_STEP_SECONDS`) are
behavioral tuning constants in `RmsWaveformMath`, the `EdgeGlowUniformMath` provenance convention —
never design tokens (geometry, `BarWidth`/`BarGap`/`MaxBarHeight`, stays in `AuraSpacing.Composer`).

**Numerical-stability trap (found via on-device redroid capture, not the unit suite):** the
critically-damped spring is numerically stiff at `SPRING_HZ` — a single semi-implicit-Euler step at
the `MAX_STEP_SECONDS` clamp value overshoots the target and clamps every bar to the ceiling
(verified: from rest toward a mid-range target, one step at `dt = 1/20` overshoots to ~1.59 before
the `coerceIn` clamp), so a slow/janky frame (redroid's software renderer; equally a real dropped
frame or GC pause on a Pixel) reproduced the EXACT SAME "triangle" symptom in a new form — every bar
uniformly maxed out instead of a tapered, traveling pseudo-spectrum. The `dtSeconds` clamp alone
only bounds how much simulated time one call advances; it does not make the integration stable.
`stepBars` now internally sub-steps at `SUB_STEP_SECONDS` (1/60s) regardless of how large the
caller's (already-clamped) `dtSeconds` is, looping the per-bar spring+coupling update until the full
clamped duration is consumed. Confirmed with a Python port of the exact formula before and after:
one `dt = 1/20` step from rest toward target `0.5` produced `h = 1.59 → clamped 1.0` pre-fix, `h =
0.36` (smooth, no overshoot) post-fix. Pixel-sampled on-device frames (fixed build) show the peak
bar index genuinely shifting between captures (~150ms apart) rather than a static or
uniformly-saturated row.

## C-state morphs (spec §6.2 Morphs)

`ComposerState` resolution priority, encoded in `ComposerState.resolve` (pure, unit-testable without
Compose): dictation wins over everything (an explicit, exclusive user action) → an in-flight run
wins over plain typing (`Streaming` must render even with an empty draft, to show the stop
affordance) → draft text alone decides `Typing` vs. `Idle`. Every state transition animates via the
SAME `ComposerMorphTransform` (`AuraMotion.composerMorphSpring`, 200ms crossfade) — "one cluster,
not per-state composables that pop." `trailingAccessory`, when supplied, replaces the ENTIRE trailing
cluster (not just the action circle) — the assist overlay needs the whole slot for its own
mic-circle/docked-orb/bare-glyph three-way (`ui/overlay/CLAUDE.md`); `AuraComposer` itself stays
agnostic to what a caller puts there.

## Mic circle tokens (`ComposerOverlayMicCircle`, overlay style only)

`AuraColors.accentOverlayMic` = **`#709FF8`** (measured; supersedes the Rev E estimate
`#A9C7FF`, notably lighter/whiter than reality). Glyph tint `onAccentOverlayMic` = `surfaceCanvas`
(pure black) — both fully opaque, confirmed strong contrast against `surfaceOverlayPill`'s
`#020B2B` fill; this pairing was independently ruled out as a source of the "mic occasionally
invisible" bug during the (the actual root cause was a stale-composition-state
bug, `ui/overlay/CLAUDE.md`'s draft-clear-on-Idle law — not a rendering/contrast defect). The
Docked-style mic entry point is a bare glyph (`ComposerBareIconButton`), not a filled circle — the
filled-circle treatment is overlay-only, matching D-1's "the overlay pill gets its own C5 anatomy"
ruling (Rev E §E-3, frame F3).
