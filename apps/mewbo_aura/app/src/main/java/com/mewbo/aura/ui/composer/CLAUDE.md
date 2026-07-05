> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Composer — ui/composer/

Scope: `ui/composer/` — `AuraComposer` (one component, five `ComposerState`s, consumed by both the
docked in-app chat and the assist overlay), `ComposerState`/`ComposerStyle`, `RmsWaveform`,
`ComposerOptionsSheet`. Spec §6.2 (C1-C5), Rev E §E-3 frame corrections, Gitea #181 item 6 (overlay
margin correction).

## Two pill widths, two tokens, never share one

`ComposerStyle.Docked` (in-app chat) and `ComposerStyle.FloatingOverlay` (assist overlay) render
the SAME five `ComposerState`s and anatomy, but their horizontal margin is a DIFFERENT, independently
measured token:

- `AuraSpacing.Composer.horizontalMargin` = **16dp** — docked, in-app.
- `AuraSpacing.Composer.overlayHorizontalMargin` = **38dp** — floating overlay pill (Gitea #181
  item 6, pixel-sampled from real-device captures; the original spec estimate of 16dp for the
  overlay was wrong).

These are deliberately TWO separate `AuraSpacing.Composer` fields, not one token consumed with a
style-conditional multiplier — a future in-app margin change must never accidentally drag the
overlay pill's margin along with it, or vice versa. `ComposerStyle.pillColor` (Docked →
`surfaceInput`, FloatingOverlay → `surfaceOverlayPill`) is the analogous existing precedent for
"same component, two independently-tokened style axes."

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

`AuraColors.accentOverlayMic` = **`#709FF8`** (Gitea #181 measured; supersedes the Rev E estimate
`#A9C7FF`, notably lighter/whiter than reality). Glyph tint `onAccentOverlayMic` = `surfaceCanvas`
(pure black) — both fully opaque, confirmed strong contrast against `surfaceOverlayPill`'s
`#020B2B` fill; this pairing was independently ruled out as a source of the "mic occasionally
invisible" bug during the #181 investigation (the actual root cause was a stale-composition-state
bug, `ui/overlay/CLAUDE.md`'s draft-clear-on-Idle law — not a rendering/contrast defect). The
Docked-style mic entry point is a bare glyph (`ComposerBareIconButton`), not a filled circle — the
filled-circle treatment is overlay-only, matching D-1's "the overlay pill gets its own C5 anatomy"
ruling (Rev E §E-3, frame F3).
