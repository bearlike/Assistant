> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Shader Family — ui/aurora/

Scope: `ui/aurora/` — `AuroraEdgeGlow` (the bottom bloom: the overlay's, AND the in-app chat's sole
liveness layer via `ChatScreen`), `AuroraWashTop` (**showcase-only** top wash — rendered ONLY by the
debug `LivenessShowcase`, NEVER in chat; see "Solid-resting" below), `AuroraState`/`EdgeGlowState`
(state unions), `OverlayScrim` (context-preserving 3-stop gradient scrim, 8%→55% bottom-weighted —
supersedes the flat 60%-black Rev E fill, DESIGN.md §4 [R4]). AGSL (`RuntimeShader`, API 33+). Spec:
§3.3/M1-M4/§7.0; rebuilt
wholesale against four real-device captures — every rule below is a postmortem of a
defect those captures caught that the earlier frame-derived version didn't. Canonical liveness
contract + token values: [`DESIGN.md`](../../../../../../../../../DESIGN.md) §5.

## Rule 1 — no radial/circular masks, ever

The pre-v5 `AuroraEdgeGlow` grew a CIRCLE from bottom-center (radius `res.x*0.5`) to animate
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

## Rule 3 — dither is MANDATORY, is ONE primitive, must RUN, and must run AFTER the premultiply

**The artifact this rule exists to prevent — the sliding vertical spine (root cause, confirmed by
render).** `decayLength` is a function of **x only** (the reach wave carries no `fragCoord.y` term),
so the wave's crest sits at the SAME x on every row. Quantizing the near-black ramp to 8 bits bends
every iso-luminance contour into a chevron, and every chevron's APEX lands on that one x — hundreds
of vertically-aligned apexes read as a single crisp vertical spine. It SLIDES because the noise's
x-argument is offset by `iTime` (measured ~28–34 px/s). **Quantization creates the seam; the wave
merely concentrates it into one line.** So the seam is a DITHER failure, not a geometry or a tuning
failure — and the dither that should have prevented it was broken in the four ways below. Do not
chase this artifact with wave frequency/amplitude tokens: retuning only moves the spine.

Four ways this has now shipped wrong; all four are the same rule.

1. **Dead import.** The pre-v5 shader `import`ed `GlslNoise.GLSL_CORE` and never called it — zero
   dithering ever executed, hence visible 8-bit banding on every dark falloff. The fix isn't "add
   dithering," it's "grep that the dither call site is actually reachable from `main()`."
2. **Two hand-rolled copies, two different formulas.** `AuroraEdgeGlow` used a `hash21` at 2/255;
   `AuroraWashTop` used `valueNoise(fragCoord * 0.5)` at 3/255 — value noise is spatially
   CORRELATED, which is the one thing a dither must never be (it smears banding into blotches
   instead of breaking it). A dither is a shared primitive, not a line you retype per shader.
3. **The hash was numerically degenerate exactly where the glow lives.** `hash21` opens with
   `fract(p * float2(123.34, 456.21))`; fed raw `fragCoord`, at the BOTTOM of a 1080×2400 display
   `y*456.21 ≈ 1.09e6`, where the float32 ULP is 0.125 — `fract()` retains ~3 bits, i.e. ~8 distinct
   values. The mandatory dither collapsed into a low-entropy repeating pattern precisely at the
   brightest part of the bloom, and the contouring survived.

4. **It was added to the UNPREMULTIPLIED colour — which neuters it.** This is the subtle one, and
   it is why a dither can be present, reachable, correctly shaped, and STILL fail on real glass.
   Skia surfaces are premultiplied: what reaches the framebuffer is `rgb * alpha`. Both shaders did
   `col += dither;` and then `return half4(col * alpha, alpha);` — so the perturbation was multiplied
   by alpha on the way out, giving an effective amplitude of `alpha` LSB, not 1 LSB. It was therefore
   strongest at the PEAK (steep ramp, banding invisible) and vanished in the FAINT REACHES (flattest
   ramp, widest contour bands, banding most visible) — exactly backwards.

**Current pattern (the ONE canonical form, both shaders):**
`return half4(ditherPremul(col * alpha, alpha, fragCoord), alpha);`

`GlslNoise.ditherPremul` dithers the PREMULTIPLIED rgb at **±0.5 LSB** and clamps to `alpha` (not to
1.0 — clamping to alpha is what keeps the result a valid premultiplied colour). It is built on `ign`
(interleaved gradient noise, Jimenez), whose dot product keeps `fract()`'s argument small so
precision holds at any fragCoord. ±0.5 LSB matches Skia's own production dither
(`src/gpu/DitherUtils.cpp`, `DitherRangeForConfig` → 1/255 for RGBA_8888); the "uniform dither needs
2× amplitude" result applies to WHITE noise, whereas IGN is ordered/low-discrepancy and fully
resolves a ramp at ±0.5. Keep all dither/coordinate math in `float` — AGSL `half` is fp16 (max 65504)
and cannot even represent `fragCoord.y * 456.21`. Never offset the dither by time: that is a TAA
trick, and with no temporal accumulation to average it the grain visibly crawls.

**The landing checklist — (a) and (b) alone PASSED on the shipped shader and still missed this bug.**
Before landing any new dark-gradient shader here, confirm the dither is:

- **(a) present** — the shared `ditherPremul`, never a retyped hash, never a private copy; and
- **(b) reachable** from the value that actually reaches `return half4(...)`; and
- **(c) EFFECTIVE — applied AFTER the alpha multiply and clamped to `[0, alpha]`.** *Reachable is not
  the same as effective.* A dither on the unpremultiplied colour is scaled by alpha on the way out
  and vanishes in exactly the faint region where the contours are widest. Measured on the shipped
  shader: correctly scaled only in the bottom ~8% of the screen; 800px up it was ±0.124 LSB, a
  QUARTER of what a dither needs, and flat identical-value runs of 20–34px survived on-device.

The `clamp(…, 0.0, alpha)` is **load-bearing, not decoration**: these shaders draw a full-screen
`drawRect`, so without it the dither would paint ±1 LSB of static noise across the ENTIRE screen
where `alpha == 0` (a visible haze on OLED). Clamping to `[0, alpha]` both preserves the
premultiplied invariant (Skia enforces `RGB <= A`) and self-zeroes the dither as alpha → 0. That
clamp lives INSIDE `ditherPremul` precisely so no call site can forget it.

**Expected, correct side effect — do NOT report it as a new bug.** The chat ramp spans only ~50–84
distinct 8-bit codes. A correct dither necessarily trades wide flat CONTOURS for fine GRAIN. Grain is
the right answer at this bit depth; a "smooth" un-dithered ramp at these code counts is exactly what
produces the spine.

**Alpha is deliberately NOT dithered.** It is quantized to 8 bits too, but the compositor computes
`premul + dst*(1-alpha)`, so a 0.5/255 alpha error perturbs the output by only `dst * 0.5` LSB —
against this family's near-black canvas (dst ≈ 14/255) that is ~0.03 LSB. Below anything visible;
adding an alpha dither would be gold-plating.

**The platform ALSO dithers these draws.** Compose's `DrawScope` paint is
`android.graphics.Paint(7)` = `ANTI_ALIAS | FILTER_BITMAP | DITHER_FLAG`
(`AndroidPaint.android.kt: makeNativePaint`), and Skia dithers any non-constant shader on RGBA_8888
(`SkPaintPriv::ShouldDither`) — a `RuntimeShader` is non-constant. Verified in pixels: `AuraSpark`,
which has NO app-level dither, still renders an ordered ±1 LSB lattice in locally-flat regions while
its alpha=0 background is exactly `[0]`. So the app-level term is belt-and-braces, and it only holds
while we draw via `ShaderBrush`/`drawRect` in a `DrawScope` — a future move to a `RenderEffect` or a
`graphicsLayer` would silently drop the platform's half of it.

## Rule 3b — the noise interpolant is quintic (a QUALITY choice, not a bug fix)

`valueNoise` interpolates with `f*f*f*(f*(f*6-15)+10)` (the "improved Perlin" quintic) rather than
the classic `f*f*(3-2f)` smoothstep. Keep it — it is free, standard, and strictly smoother.

**Do NOT cite it as the cause of any on-device artifact.** The cubic Hermite is already C1
(continuous value AND first derivative); only its SECOND derivative jumps at a lattice boundary.
Mach banding requires a FIRST-derivative discontinuity, and there is no perceptual result supporting
a visible line from a C2-only kink. Perlin's own reason for the quintic was smooth normals when noise
is consumed in DERIVATIVE space (bump/displacement mapping), which is not what this family does. An
earlier version of this file claimed the quintic fixed a "sliding vertical Mach band" — that
postmortem was wrong and has been removed rather than left to be re-learned as lore.

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

## Rule 4b — aurora hue field: bounded aperiodic noise-drift, NOT the outlawed angular wheel ([R5])

Rule 4 removed a PERIODIC ANGULAR hue wheel (a phase uniform rotating through 4 stops) because it
let the wrong hue periodically dominate. [R5] (device feedback: "make it a pretty multi-hue aurora —
blue + violet + ember") re-introduces multiple hues WITHOUT re-introducing that bug class. The
distinction is load-bearing and must survive any future edit here:

- The family the two-stop pair comes from drifts through THREE stops (A = `auroraOverlayLiveBloom`
  blue → B = `auroraOverlayVioletBloom` violet → C = `auroraOverlayEmberBloom` ember) sampled by a
  slow, **bounded, APERIODIC value-noise field** over (x, y, iTime×rate). Noise is bounded in [0,1]
  and never rotates — there is no angle, no phase uniform, nothing that can wrap around and let a
  hue periodically dominate. Violet sits BETWEEN blue and ember so the A→B→C blend path never
  crosses muddy gray.
- **INTENSITY still selects light-vs-deep within the sampled family and still drives alpha** — Rule
  4's alpha/hue coupling is fully intact. The drift picks WHICH family; `glow` still picks where in
  that family's light↔deep ramp you are (`mix(hueDeep, hueLight, glow)`, Rule 6 order preserved).
  The B/C families are plain two-stop pairs the shader sees as float3 uniforms — no 3rd stop in any
  single `mix`, no angle term (the multi-hue is spatial/temporal via the noise field, not an
  angular sweep).
- **`iHueDrift == 0` is the chat byte-path.** At 0 the family blend collapses to the pure A pair
  (`mix(a, b, 0) == a`) AND the hue-noise sample is BRANCH-SKIPPED entirely (`if (iHueDrift > 0.0)`)
  — chat renders the legacy two-stop navy, single-hue, zero extra ALU. `ChatScreen` passes no
  `hueDriftAmount`; only the overlay (and the debug showcase) pass 1f. Verify with the chat
  bottom-edge no-drift pixel check (single-hue navy, b>g>r everywhere) on any change here.
- **Reduced motion keeps the FIELD, drops only the travel.** `iHueDrift` is NOT reduced-motion-gated
  — the drift SPEEDS (`iTime × iWaveSpeedHz`) are already 0 there, so the noise field freezes into a
  STATIC multi-hue frame (device-verified: two captures 3s apart, 0/154440 glow pixels changed,
  still violet+ember+blue across x). This is the same "field stays, motion stops" shape as the
  breathe/wave.
- The family blend is written branch-free (`mix(mix(A,B,segAB), C, segBC)` with `segAB =
  clamp(hueT·2,0,1)`, `segBC = clamp((hueT−0.5)·2,0,1)`) rather than a `hueT < 0.5 ? … : …` ternary
  on a float3 — the ternary-with-vector form risks a runtime-compiler failure that renders the whole
  shader black; the branch-free composition is bit-for-bit identical and compiles on SwiftShader.
- Verify hues across SPACE and TIME (the Rule-4 "sample across cycles" trap generalizes): a single
  frame can sit in a locally-monochrome region of the aperiodic field. Device-verified multi-hue:
  one steady Listening frame read violet center (`130,101,183`), ember right rail (`104,68,86`,
  r>b), blue left (`60,47,91`) simultaneously — R/B ratio climbing 0.66→1.21 across x.

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
`EdgeGlowState.Resting` ([R4], overlay-scoped) adds a THIRD local ratio pair against the same
token, not a new measured value: `OVERLAY_RESTING_DECAY_FRACTION` (0.45) sits between Listening's
full reach and Thinking's contracted hug, and `OVERLAY_RESTING_INTENSITY` (0.55) dims it — both
private constants in `EdgeGlowUniformMath`, reusing Listening's `centerWeightStrength` since
Resting is presence, not corner-suppression, unlike Thinking.

## Rule 6 — a reach measurement is NOT a decay length: back-solve it

The first cut set `auroraOverlayBloomDecayDepth = 45dp` by reading "visible to ~143dp at the
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
  would jump the drift (the speedScale trap below). The overlay passes its own short `dismissFadeMs`
  (`OVERLAY_GLOW_DISMISS_MS`, `ui/overlay/CLAUDE.md`'s dismiss-choreography section) rather than the
  default `VISIBILITY_TRANSITION_MS` — same caller-knob mechanism, never a fork. `reducedMotion` still
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
  (DESIGN.md §7.9; first hit the orb square edge, then the bottom glow's own falloff). Monotonic
  smoothstep, no plateau (Rule 2 intact); the mandatory dither still runs. It is a no-op for
  small-reach bottom-anchored callers (the overlay, `reachScale` 1) whose bloom is already ~0 that
  high. `TOP_FADE_FRACTION` is a BEHAVIORAL TUNING CONSTANT (below), not a measured token.

## Perimeter bloom (R4) — the invocation ignite → settle envelope

The overlay's invocation moment ([R4 2026-07-10]) is a one-shot PERIMETER bloom that
rides `AuroraEdgeGlow`'s entrance and decays into the bottom-only live glow. Its whole job is to
make "the assistant just woke up" ownable brand light without adding a second shader or a radial
mask. Four laws hold it inside this family's existing rules:

- **Edge-anchored terms PEAK at the draw bounds — that is not a §7.9 seam.** The perimeter is three
  edge-anchored exponentials in `AURORA_EDGE_SHADER_SRC` — a per-column `sideGlow` from the L/R
  edges, a `bottomBias` (`mix(0.25, 1.0, heightFrac²)`, ~25% at top → full at bottom) so the light
  reads as EMANATING from the pill, and a faint `topGlow` from the top edge. Rule 1 still holds (no
  radial mask — same construction as the bottom term, rotated 90°). The top-fade window
  (`smoothstep(0, res.y·iTopFadeFraction, y)`) scopes to the BOTTOM-ANCHORED `vGlow` term ONLY, NOT
  the perimeter: §7.9 forbids a *clipped mid-falloff* (a falloff still bright where the surface
  clips, leaving a mid-slope value the clip slices a hard line across). An edge-anchored term that
  PEAKS at the bound has no beyond-the-bound region to step against — exactly like the bottom glow
  peaking at the bottom bound reads as a light source, not a seam. Device-verified: the top draw
  bound sampled 0.0 luminance in 14/16 overlay frames, a faint transient corner tint (~9–14/255)
  only during the single peak-bloom frame, and pure 0 in every settled frame — no persistent line.
  The perimeter's inner boundary undulates because `sideDecay` derives from `decayLength`, which the
  existing `valueNoise` wave already modulates — zero new noise terms (Rule 3's dither still runs).
- **The bloom envelope is CALLER-owned.** `AuroraEdgeGlow(perimeterBloom: State<Float>? = null)` is a
  0..1 value the CALLER animates — the composable only turns it into pixels, the same externally-
  driven pattern as `EdgeGlowState.Igniting`'s progress and the orb's `rmsDb`. The overlay's
  `AssistOverlayScreen.rememberPerimeterBloom` owns the timeline: snap to 1 on leaving Idle (the
  alpha rides `AuroraEdgeGlow`'s own `iVisible` ramp), hold through the 450ms ignite
  (`AuraMotion.edgeSweepMs`), exhale to 0 over `AuraMotion.bloomSettleMs` (950ms, FastOutSlowIn — an
  exhale, not a linear wipe), reset on Idle per the overlay's per-invocation reset law. Read in the
  DRAW phase only (like `visible.value`/`timeSeconds`) so the per-frame value never subscribes the
  composable to recompose. `null`/0 = no bloom; `ChatScreen` never passes it, so chat is
  byte-identical (verified: the shader collapses to `glow = vGlow` when `iPerimeterBloom == 0`).
- **The ignition color pair lerp is CPU-side (Rule 4 intact).** At bloom=1 the pair lerps to
  `AuraColors.auroraIgnitionBloom` (brighter, `accentPrimary`-adjacent brand blue); at bloom=0 back
  to the caller's own `colors` (the overlay's `auroraOverlayLiveBloom`; chat's darkened pair). The
  lerp happens in Kotlin (`androidx.compose.ui.graphics.lerp`) before the uniform set — the shader
  still sees a plain two-stop `mix(deep, light, glow)`, never a 3rd stop or an angle term. Caution:
  Compose's `Color.lerp` round-trips through Oklab, so `t=0` byte-identity to the pre-lerp color is
  value-specific (~1% of arbitrary color pairs drift 1 LSB) — retuning either color pair technically
  breaks strict byte-identity with pre-lerp behavior (imperceptible under the mandatory dither), so
  don't cite byte-identity as an invariant here after a retune.
- **Reduced motion: belt AND suspenders.** `rememberPerimeterBloom` never raises the envelope when
  `reducedMotion` (the timeline branch is guarded), AND `AuroraEdgeGlow` forces `bloom = 0f` in the
  draw phase regardless of what a caller passes. Device-verified with
  `animator_duration_scale 0`: no bloom burst at the invocation moment, a static bottom glow, top
  edge 0.0 in every frame.

`EdgeGlowState.Resting` (OVERLAY-scoped, [R4]) is the state the bloom settles toward when the first
turn is done (`Streaming.done`) but the overlay is still on screen — a low, STATIC (no breathe —
oscillation would read as still "doing something") bottom pool between Thinking's contracted hug and
Listening's wide reach, signalling "the assistant is present." **NEVER used by the chat surface**:
`ChatScreen` maps nothing to `Resting`, and the §7.2 solid-resting law (chat rest paints nothing)
is untouched — an on-screen overlay is a live surface; the chat surface's rest is not.

## Fluid edge-lit perimeter ([R5]) — persistent floor + 2-octave + liquid level

Device feedback on 0.0.30-debug: the overlay glow was "not strong towards the edges" and "not
fluid." Three additive mechanisms, all inside the existing rules (edge-anchored terms, phase-
continuous motion, chat byte-path):

- **Persistent perimeter floor.** Before [R5] the perimeter collapsed to zero once the bloom
  settled (`perimeter × iPerimeterBloom`), so the live states were bottom-only. Now
  `perimeter × max(iPerimeterBloom, iPerimeterFloor)` — a live bloom still wins (it's ≥ any floor),
  but after it settles the floor keeps a faint side/top presence so the state reads edge-lit.
  `iPerimeterFloor = EdgeGlowUniformMath.perimeterFloor(state) × perimeterPresence`, an exhaustive
  per-state `when` (Listening/Igniting 0.35 · Resting 0.30 · Thinking 0.15 · Hidden 0 — Thinking
  lowest because its contracted, corner-suppressed hug is a designed state, not an edge-lit one).
  The floors are BEHAVIORAL TUNING CONSTANTS (ratios of the bloom's full perimeter strength), never
  promoted to `AuraColors`. `perimeterPresence` is the caller knob: overlay 1f, **chat default 0f**
  → `iPerimeterFloor` 0 → the perimeter block zeroes (`max(0,0) == 0`) and chat stays bottom-only,
  byte-path. NOT reduced-motion-gated (a static edge presence, not travel). Device-verified: steady
  Listening side rails lit `~66–128` (blue left, ember right) where pre-[R5] Listening rails were
  ~0; chat side points stayed dim `~19–25` navy (bottom-only proven).
- **2-octave reach wave.** The single `valueNoise` reach wave became a coarse swell (0.65) + a
  finer, faster octave (0.35, 2.3× spatial freq / 1.9× speed) so the boundary reads as liquid, not
  one sine-like undulation. Both are `iTime × rate` (§7.12 phase-continuous). This is a SHARED term
  — it modulates `decayLength` for every caller including chat (a fluidity improvement, not a
  hue/perimeter drift; the chat byte-path guard is specifically the hue+perimeter branch-skip, not
  the wave).
- **Per-row liquid level.** A second value-noise wave over `y`+time modulates the side reach
  (`sideDecay`) so the edge light visibly rises and falls ALONG the edges (the "liquid level" read),
  scaled by the same `iWaveAmplitude` token (0 under reduced motion → static). Phase-continuous.

All three are additive `valueNoise` samples (+3/px worst case incl. the Rule-4b hue field, that one
branch-skipped at `iHueDrift 0`) — accepted on SwiftShader for geometry checks (never FPS). Rule 3's
mandatory dither still runs; edges still PEAK at the draw bounds (no §7.9 seam, same as the R4
perimeter).

## Tuning-constant provenance — measured token vs. behavioral ratio

Every `AuraColors`/`AuraSpacing` value consumed here must be traceable to an actual capture
(pixel-sampled hex, measured dp/px reach) — that's a MEASURED token. A value that only exists to
relate two measured tokens to each other (e.g. `THINKING_DECAY_FRACTION`, `THINKING_INTENSITY`,
`WAVE_AMPLITUDE`) is a BEHAVIORAL TUNING CONSTANT — it lives as a private constant in
`EdgeGlowUniformMath`/local to the shader file, never promoted to `AuraColors`/`AuraSpacing`, and its
KDoc must say what it's a ratio/multiplier OF. Don't invent a third category; if a value doesn't fit
either bucket, it's not ready to land yet — go re-derive it from a capture or drop it.

## Three time-based traps (moved from ui/CLAUDE.md — apply to any shader/animation work here)

- **The frame clock is milliseconds since BOOT — never narrow it to Float raw.**
  `withInfiniteAnimationFrameMillis` yields uptime, not session time. On a phone up 5 days that is
  ~4.3e8 ms, which lands in a binade where the float32 ULP is **32 ms** — so `timeSeconds` only
  advances every OTHER frame and the wave JUDDERS in ~1.1px jumps instead of drifting ("not fluid");
  by ~11 days the ULP doubles again into 4-frame stalls. It degrades with UPTIME, which is why
  redroid never shows it (containers live for minutes) and only a real phone does.
  `rememberShaderTimeSeconds` anchors to the first observed frame **in `Long`, where the subtraction
  is exact**, and narrows only the small difference; the origin sits in a `remember` OUTSIDE the
  `LaunchedEffect(isVisible)` so a lifecycle pause/resume RESUMES the phase rather than resetting it.
  This is the same bug class fixed at one call site — this is the shared seam that fix missed.
  It is safe for every consumer because they are all delta-based or sine-of-t
  (`ShaderRotationAccumulator.advance` integrates `dt`, `EdgeGlowTransitionTracker` and `Orb`'s
  `stateEnteredAt` subtract an origin, the wash takes `% 1000f`) — verify that before adding a
  consumer that wants an absolute clock, because none exists today.

- **`LaunchedEffect(transitionKey)` ordering race**: a transition-tracking effect and
  `rememberShaderTimeSeconds()`'s own internal `LaunchedEffect` both launch on the same composition
  pass with no guaranteed relative order — the tracker can read `timeSeconds` before the frame
  clock's first tick, capturing 0 instead of the true absolute-clock time and silently re-breaking
  "state X at the transition moment." Fix: track the transition in the DRAW phase, not a
  `LaunchedEffect` — see `AuroraEdgeGlow.kt`'s `EdgeGlowTransitionTracker`, read exclusively inside
  `onDrawBehind`, which strictly follows that frame's `timeSeconds` update and is therefore
  race-free. `Orb.kt`'s own `OrbUniformMath` error-pulse envelope has the IDENTICAL latent pattern,
  still unfixed (low-visibility — only shifts the pulse decay's starting phase — but
  real; fix it the same way if it's ever touched).
- **Verify animated properties across TIME, not just at t=0**: the pre-v5 hue-rotation bug (Rule 4)
  was checked analytically against the static geometry only and looked safe — the ROTATING phase
  riding on top of it periodically let the wrong hue own the visible band. A t=0 check or a single
  screenshot cannot catch this. Sample across several full cycles of any oscillating uniform
  (`LivenessShowcase`, below) before calling a shader change verified — this is exactly how the
  hue-rotation bug was finally caught, and exactly why it can't recur under the new two-stop design
  (there's no oscillating angle/phase term left to sample).

## Verification host

`LivenessShowcase` (debug-only, `ui/aurora/LivenessShowcase.kt`) is the one place all `AuroraState`/
`EdgeGlowState` values are exercised side-by-side without a live backend — its "Bottom glow" page was
retired with `AuroraGlowBottom`'s deletion; the Wash page now exercises all four
`AuroraState` values including `Resting`. Any new state or shader parameter needs a page/variant
here before it can be called verified — a single dev-build screenshot is not enough (see the
verify-across-time trap above). redroid's software GPU (SwiftShader) is fine for judging
geometry/crispness here; never judge FPS on it (`apps/mewbo_aura/CLAUDE.md` device matrix).
