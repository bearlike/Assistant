> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Shader Family — ui/aurora/

Scope: `ui/aurora/` — `AuroraEdgeGlow` (the bottom bloom: the overlay's, AND the in-app chat's sole
liveness layer via `ChatScreen`), `AuroraWashTop` (**showcase-only** top wash — rendered ONLY by the
debug `LivenessShowcase`, NEVER in chat; see "Solid-resting" below), `AuroraState`/`EdgeGlowState`
(state unions), `OverlayScrim`. AGSL (`RuntimeShader`, API 33+). Spec: §3.3/M1-M4/§7.0; rebuilt
wholesale against four real-device captures for Gitea #181 — every rule below is a postmortem of a
defect those captures caught that the earlier frame-derived version didn't. Canonical liveness
contract + token values: [`DESIGN.md`](../../../../../../../../../DESIGN.md) §5.

## Rule 1 — no radial/circular masks, ever

The pre-#181 `AuroraEdgeGlow` grew a CIRCLE from bottom-center (radius `res.x*0.5`) to animate
ignition. A circle under-covers the diagonal to the corners by construction: >40% brightness loss
starting ~220px in from each edge, exact zero within ~88px of the corners — verified numerically,
confirmed on-device ("cropped in a circular arc," bug 1). Fix, not a patch: there is no radial mask
at all now — ignition instead grows the vertical exponential falloff's OWN reach (`decayLength`)
from near-zero to its resting depth. That growth covers every column of the screen at the same
rate, so there is nothing left to crop against a diagonal. Any new ignite/reveal animation in this
family must grow a per-column falloff parameter, never a radius from a point.

## Rule 2 — continuous exponential decay, no plateau

`verticalGlow = exp(-distFromBottom / decayLength)`, peaking at `distFromBottom = 0` (the true
screen edge) and decaying continuously — no flat 100% plateau, no hard feather edge. The old shader
was `0..624px @ 100% + 168px feather`: a flat plateau gave the opaque composer pill a sharp bright
line to crop across when it sat on top of the glow ("opaque/banded, not a smooth blend," bug 2) —
peaking at the very edge (mostly hidden below/behind the pill) and decaying from there leaves no
bright plateau for anything opaque to slice a visible line through. This is the general fix for any
future occlusion-by-opaque-chrome artifact in this family: never let a decorative layer plateau
under UI that's meant to crop it.

## Rule 3 — dither is MANDATORY on any dark gradient, and must actually run

The old shader `import`ed `GlslNoise.GLSL_CORE` and never called it — a dead import, zero dithering
ever executed, hence visible 8-bit banding on every dark falloff. The fix isn't "add dithering," it's
"grep that the dither call site is actually reachable from `main()`." Current pattern (both shaders):
a per-pixel `hash21(fragCoord)` term, uncorrelated with any spatial/temporal noise already driving
the waviness, added to the final color: `col += (hash21(fragCoord) - 0.5) * (2.0 / 255.0)`. Before
landing any new dark-gradient shader in this family, confirm the dither term is (a) present and (b)
wired into the value that actually reaches `return half4(...)` — an unused import is not a fix.

## Rule 4 — hue blends by the glow's own intensity, never by angle/position

The old `AuroraEdgeGlow` was a 4-stop angular wheel (blue/green/clay/violet) with a phase uniform —
it had a periodic bug where the wrong hue could dominate the visible band (confirmed via capture,
died twice across two review rounds before this rebuild). Every real capture (9 columns across two
live states) reads blue-family only, zero green/clay/violet anywhere. Both shaders now derive color
from a plain two-stop `mix(colorLight, colorDeep, intensity)`, where `intensity` is the SAME bounded
value already driving alpha — there is no separate angle/phase uniform left to drift out of sync,
which removes the whole hue-rotation bug class by construction rather than tuning the wheel harder.
`AuraColors.auroraOverlayBloom`/`auroraWashTop` are the two-stop token lists; do not reintroduce a
3rd/4th stop or an angle-driven mix function without new capture evidence. Stop ORDER is measured
too: the bloom's bright bottom edge is the PALE stop, fading through deep (`mix(deep, light, glow)`
— see Rule 6); inverting it read wrong against every capture in the first cut.

## Rule 5 — state-dependent reach is measured per state, not one shared constant

`EdgeGlowState.Listening` (idle-listening) reads WIDE: measured faint-but-real reach into the bottom
corners, visible to ~143dp+ at the screen edges. `EdgeGlowState.Thinking` (streaming/generating)
contracts to hug the pill with NO corner reach. Both states still peak at the true bottom edge
(Rule 2 is state-independent) — only `decayLength` (`EdgeGlowUniformMath.decayDepthDp`) and how hard
corners get suppressed (`centerWeightStrength`) differ. A contracted decay length ALONE cannot
darken corners, because the bottom-left/right corners sit on the SAME `distFromBottom=0` row as the
peak — Thinking therefore also needs a much stronger `centerWeight` to read as "no corner reach"
despite still peaking there. `AuraColors.auroraOverlayBloomDecayDepth` (45dp) is the ONE measured
token (Listening's wide reach); Thinking's contraction is a local behavioral ratio
(`THINKING_DECAY_FRACTION = 0.22f`) against that token, not a second measured constant.

## Rule 6 — a reach measurement is NOT a decay length: back-solve it

The first #181 cut set `auroraOverlayBloomDecayDepth = 45dp` by reading "visible to ~143dp at the
edges" as if reach were the exponential's length parameter. An exponential with L=45dp is at 4% by
143dp — combined with a halved peak alpha, the entire effect rendered INVISIBLE behind the overlay's
opaque bottom chrome ("aurora renders black", the device-gate finding; channel-encoded diagnostic
shader confirmed every term healthy, i.e. tuning, not code). The law: given a scanline sample at
distance d still showing fraction r of peak, `L = -d / ln(r)` (ref2 edge column: r≈0.36 at 143dp →
L≈139dp). Corollaries, both measured: peak alpha back-solves near-opaque at the bottom edge
(#6D85B9 over #2A2A2E scrim), not the 0.5 "glow, never a stripe" eyeball-era cap — stripe-avoidance
comes from the continuous falloff, not from starving the peak; and the hue order is PALE at the
bright bottom edge fading through deep (`mix(deep, light, glow)`), not the reverse. When a shader
"renders black/nothing" with zero compile errors, suspect tuning-vs-occlusion before pipeline: the
channel-encoding diagnostic (R/G/B = one suspect term each, alpha=1) answers it in one build.

## Run-state liveness — solid at rest, ONE glow layer (ChatScreen)

> **Regression note:** an always-on `Resting` top wash in chat shipped and was reverted the SAME day
> (2026-07-04) — with it went the transparent top bar. See DESIGN.md §7 (§7.2 wash, §7.3 top bar).
> The wash constants that version added (`RESTING_INTENSITY_FRACTION`, `ACTIVE_DRIFT_BOOST`) are
> DELETED; do not reintroduce them or any chat-surface wash. `AuroraWashTop` reverted to its
> pre-regression form and now renders ONLY in the debug `LivenessShowcase`.

User directive 2026-07-04 ("always tell running from dead") — DESIGN.md §5:

- **Solid-resting law.** The app's native resting state is a SOLID background: NO aura, transparent
  top bar. "Running vs dead" is legible precisely because the resting state paints nothing — a
  visible glow means live, its absence means idle. Do not add an ambient/decorative layer that
  survives into rest.
- **ChatScreen sole-owner law.** In chat there is exactly ONE liveness layer: `ChatScreen`'s bottom
  `AuroraEdgeGlow`, tuned via caller-side `IN_APP_GLOW_*` reach/center-weight/alpha overrides.
  `ChatSurface` renders NO aura of its own (it only derives a `washActive` boolean to style
  `ChatTranscript`'s `overWash`). Nothing paints behind the top bar. There is no second layer to
  double-stack against.
- **Invocation window.** On a fresh chat-screen entry the glow ambient-breathes as
  `EdgeGlowState.Listening(0f)` for `AMBIENT_INVOCATION_WINDOW_MS` (a `ChatScreen` behavioral
  constant), then flips to `EdgeGlowState.Hidden` — rest is solid thereafter. A live run overrides
  this with `EdgeGlowState.Thinking` for the whole `Sending`/`Streaming` phase.
- **Run-end ease-off (photosensitivity).** When a run ends the glow must NOT snap from `Thinking` to
  black — an abrupt on→off luminance change reads as a flash (user directive; photosensitivity/
  epilepsy). `ChatScreen` passes `AuroraEdgeGlow(dismissFadeMs = AuraMotion.edgeRestFadeMs)` (≥3s)
  and the composable eases the alpha envelope (`iVisible`) to 0 over that duration on a LINEAR curve
  (constant rate, no fast segment). It CANNOT fade by handing the shader `Hidden`'s own uniforms:
  `intensity(Hidden) = 0f` zeroes alpha instantly (which is what made the pre-existing `visible` ramp
  an instant vanish) and `Hidden`'s WIDE reach would pop the bloom. So while the fade is in flight the
  composable HOLDS the last active state's whole frame (reach, intensity, wave phase AND speed, via a
  `renderState`/`renderSpeed` latch) and ramps only `iVisible`. Holding the wave SPEED is
  load-bearing — the caller drops `speedScale` at the flip, and a rate change while `iTime` is large
  would jump the drift (the speedScale trap below). The overlay passes no `dismissFadeMs` and keeps
  the quick `VISIBILITY_TRANSITION_MS` dismiss (caller-knob, never fork). `reducedMotion` still
  `snap()`s (accessibility contract; a single monotonic fade-to-black is not a flash either way).
- **`speedScale` rate-only mechanism (phase-continuous).** The livelier active drift is
  `AuroraEdgeGlow(speedScale = IN_APP_GLOW_ACTIVE_SPEED_SCALE)` for `Thinking` only. `speedScale`
  multiplies the wave-drift RATE (`iWaveSpeedHz`) and nothing else — never the accumulated phase.
  It cannot jump the wave: the shader's phase input `iTime` is `EdgeGlowTransitionTracker`'s
  `elapsedInState`, which RESETS to 0 at every state transition, and a state transition is the ONLY
  moment `speedScale` can change (it is derived from the glow state). At `iTime == 0` the term
  `iTime * iWaveSpeedHz` is 0 for any scale, so the rate change lands exactly where the phase is
  already 0 and grows continuously from there. This is the §12 "speed changes that jump" trap
  (DESIGN.md §7.12) avoided by construction — scale the rate, never the phase.
- **Top-fade edge-window law.** A large caller `reachScale` (the in-app 5.5×) leaves the vertical
  exponential clearly nonzero at the TOP draw bound, which hard-stops into a thin horizontal seam
  where the surface clips. The shader feathers the bloom to EXACTLY 0 over the top `TOP_FADE_FRACTION`
  (0.18) of the surface height (`smoothstep(0, res.y * iTopFadeFraction, fragCoord.y)`) so there is
  no bright edge left for the clip bound to slice. This is the SAME edge-window law as the orb halo
  (`ui/CLAUDE.md` orb section: `1 - smoothstep(0.44, 0.5, dist)`) and Rule 1's no-crop-line
  principle, generalized to the top edge — alpha must reach 0 before any draw bound, always
  (DESIGN.md §7.9; first hit the orb square edge #176, then the bottom glow's own falloff). Monotonic
  smoothstep, no plateau (Rule 2 intact); the mandatory dither still runs. It is a no-op for
  small-reach bottom-anchored callers (the overlay, `reachScale` 1) whose bloom is already ~0 that
  high. `TOP_FADE_FRACTION` is a BEHAVIORAL TUNING CONSTANT (below), not a measured token.

## Tuning-constant provenance — measured token vs. behavioral ratio

Every `AuraColors`/`AuraSpacing` value consumed here must be traceable to an actual capture
(pixel-sampled hex, measured dp/px reach) — that's a MEASURED token. A value that only exists to
relate two measured tokens to each other (e.g. `THINKING_DECAY_FRACTION`, `THINKING_INTENSITY`,
`WAVE_AMPLITUDE`) is a BEHAVIORAL TUNING CONSTANT — it lives as a private constant in
`EdgeGlowUniformMath`/local to the shader file, never promoted to `AuraColors`/`AuraSpacing`, and its
KDoc must say what it's a ratio/multiplier OF. Don't invent a third category; if a value doesn't fit
either bucket, it's not ready to land yet — go re-derive it from a capture or drop it.

## Two time-based traps (moved from ui/CLAUDE.md — apply to any shader/animation work here)

- **`LaunchedEffect(transitionKey)` ordering race**: a transition-tracking effect and
  `rememberShaderTimeSeconds()`'s own internal `LaunchedEffect` both launch on the same composition
  pass with no guaranteed relative order — the tracker can read `timeSeconds` before the frame
  clock's first tick, capturing 0 instead of the true absolute-clock time and silently re-breaking
  "state X at the transition moment." Fix: track the transition in the DRAW phase, not a
  `LaunchedEffect` — see `AuroraEdgeGlow.kt`'s `EdgeGlowTransitionTracker`, read exclusively inside
  `onDrawBehind`, which strictly follows that frame's `timeSeconds` update and is therefore
  race-free. `Orb.kt`'s own `OrbUniformMath` error-pulse envelope has the IDENTICAL latent pattern,
  still unfixed as of #181 (low-visibility — only shifts the pulse decay's starting phase — but
  real; fix it the same way if it's ever touched).
- **Verify animated properties across TIME, not just at t=0**: the pre-#181 hue-rotation bug (Rule 4)
  was checked analytically against the static geometry only and looked safe — the ROTATING phase
  riding on top of it periodically let the wrong hue own the visible band. A t=0 check or a single
  screenshot cannot catch this. Sample across several full cycles of any oscillating uniform
  (`LivenessShowcase`, below) before calling a shader change verified — this is exactly how the
  hue-rotation bug was finally caught, and exactly why it can't recur under the new two-stop design
  (there's no oscillating angle/phase term left to sample).

## Verification host

`LivenessShowcase` (debug-only, `ui/aurora/LivenessShowcase.kt`) is the one place all `AuroraState`/
`EdgeGlowState` values are exercised side-by-side without a live backend — its "Bottom glow" page was
retired with `AuroraGlowBottom`'s deletion (#181); the Wash page now exercises all four
`AuroraState` values including `Resting`. Any new state or shader parameter needs a page/variant
here before it can be called verified — a single dev-build screenshot is not enough (see the
verify-across-time trap above). redroid's software GPU (SwiftShader) is fine for judging
geometry/crispness here; never judge FPS on it (`apps/mewbo_aura/CLAUDE.md` device matrix).
