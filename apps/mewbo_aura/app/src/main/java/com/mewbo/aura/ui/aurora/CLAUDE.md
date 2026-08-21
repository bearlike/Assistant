> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Shader Family — ui/aurora/

Scope: `ui/aurora/` — `AuroraEdgeGlow` (the bottom bloom: the overlay's, AND the in-app chat's sole
liveness layer via `ChatScreen`), `AuroraWashTop` (**showcase-only** top wash — rendered ONLY by the
debug `LivenessShowcase`, NEVER in chat), `AuroraState`/`EdgeGlowState` (state unions), `OverlayScrim`
(context-preserving 3-stop gradient scrim, 8%→55% bottom-weighted, DESIGN.md). AGSL
(`RuntimeShader`, API 33+). Canonical liveness contract + token values:
[`DESIGN.md`](../../../../../../../../../DESIGN.md).

Every rule below is a defect a real-device capture caught. Treat them as binding, not advisory.

## Rule 1 — no radial/circular masks, ever

A circle grown from bottom-center under-covers the diagonal to the corners by construction:
>40% brightness loss starting ~220px in from each edge, exact zero within ~88px of the corners
(numerically verified, confirmed on-device as "cropped in a circular arc"). There is no radial mask
at all now — ignition grows the vertical exponential falloff's OWN reach (`decayLength`) from
near-zero to its resting depth, which covers every column at the same rate. **Any new ignite/reveal
animation in this family must grow a per-column falloff parameter, never a radius from a point.**

## Rule 2 — continuous exponential decay, no plateau

`verticalGlow = exp(-distFromBottom / decayLength)`, peaking at `distFromBottom = 0` (the true screen
edge) and decaying continuously — no flat 100% plateau, no hard feather edge. A flat plateau gives
the opaque composer pill a sharp bright line to crop across ("opaque/banded, not a smooth blend").
Generalized: **never let a decorative layer plateau under UI that's meant to crop it.**

## Rule 3 — dither is MANDATORY, is ONE primitive, must RUN, and must run AFTER the premultiply

**The artifact this rule prevents — the sliding vertical spine.** `decayLength` is a function of
**x only** (the reach wave carries no `fragCoord.y` term), so the wave's crest sits at the SAME x on
every row. Quantizing the near-black ramp to 8 bits bends every iso-luminance contour into a chevron,
and every chevron's APEX lands on that one x — hundreds of vertically-aligned apexes read as a single
crisp vertical spine. It SLIDES because the noise's x-argument is offset by `iTime` (measured
~28–34 px/s). **Quantization creates the seam; the wave merely concentrates it into one line.** So
the seam is a DITHER failure, not geometry or tuning — do not chase it with wave frequency/amplitude
tokens, retuning only moves the spine.

Four failure modes, all the same rule:

1. **Dead import.** A shader `import`ed `GlslNoise.GLSL_CORE` and never called it — zero dithering
  ever executed. The fix isn't "add dithering," it's "grep that the dither call site is actually
  reachable from `main()`."
2. **Two hand-rolled copies, two formulas.** One used a `hash21` at 2/255, the other
  `valueNoise(fragCoord * 0.5)` at 3/255 — value noise is spatially CORRELATED, the one thing a
  dither must never be (it smears banding into blotches instead of breaking it). A dither is a
  shared primitive, not a line you retype per shader.
3. **Numerically degenerate hash.** `hash21` opens with `fract(p * float2(123.34, 456.21))`; fed raw
  `fragCoord`, at the BOTTOM of a 1080×2400 display `y*456.21 ≈ 1.09e6`, where the float32 ULP is
  0.125 — `fract()` retains ~3 bits, i.e. ~8 distinct values. The dither collapsed into a
  low-entropy repeating pattern exactly at the brightest part of the bloom.
4. **Added to the UNPREMULTIPLIED colour — which neuters it.** Skia surfaces are premultiplied: what
  reaches the framebuffer is `rgb * alpha`. `col += dither;` then `return half4(col * alpha, alpha)`
  multiplies the perturbation by alpha on the way out, giving an effective amplitude of `alpha` LSB,
  not 1 LSB — strongest at the PEAK (steep ramp, banding invisible) and vanishing in the FAINT
  REACHES (flattest ramp, widest bands, banding most visible). Exactly backwards.

**The ONE canonical form, both shaders:**
`return half4(ditherPremul(col * alpha, alpha, fragCoord), alpha);`

`GlslNoise.ditherPremul` dithers the PREMULTIPLIED rgb at **±0.5 LSB** and clamps to `alpha` (not to
1.0 — clamping to alpha is what keeps the result a valid premultiplied colour). It is built on `ign`
(interleaved gradient noise, Jimenez), whose dot product keeps `fract()`'s argument small so precision
holds at any fragCoord. ±0.5 LSB matches Skia's own production dither (`DitherRangeForConfig` →
1/255 for RGBA_8888); the "uniform dither needs 2× amplitude" result applies to WHITE noise, whereas
IGN is ordered/low-discrepancy and fully resolves a ramp at ±0.5. Keep all dither/coordinate math in
`float` — AGSL `half` is fp16 (max 65504) and cannot represent `fragCoord.y * 456.21`. Never offset
the dither by time: that is a TAA trick, and with no temporal accumulation to average it the grain
visibly crawls.

**Landing checklist — (a) and (b) alone PASSED on a shipped shader and still missed the bug.**
Before landing any new dark-gradient shader here, confirm the dither is:

- **(a) present** — the shared `ditherPremul`, never a retyped hash, never a private copy;
- **(b) reachable** from the value that actually reaches `return half4(...)`;
- **(c) EFFECTIVE — applied AFTER the alpha multiply and clamped to `[0, alpha]`.** *Reachable is
  not effective.* Measured on the broken shader: correctly scaled only in the bottom ~8% of the
  screen; 800px up it was ±0.124 LSB, a QUARTER of what a dither needs, and flat identical-value runs
  of 20–34px survived on-device.

The `clamp(…, 0.0, alpha)` is **load-bearing**: these shaders draw a full-screen `drawRect`, so
without it the dither paints ±1 LSB of static noise across the ENTIRE screen where `alpha == 0` (a
visible haze on OLED). Clamping to `[0, alpha]` preserves the premultiplied invariant (Skia enforces
`RGB <= A`) and self-zeroes as alpha → 0. It lives INSIDE `ditherPremul` so no call site can forget it.

**Expected side effect — do NOT report it as a new bug.** The chat ramp spans only ~50–84 distinct
8-bit codes. A correct dither necessarily trades wide flat CONTOURS for fine GRAIN. Grain is the right
answer at this bit depth; a "smooth" un-dithered ramp at these code counts is what produces the spine.

**Alpha is deliberately NOT dithered.** It is quantized to 8 bits too, but the compositor computes
`premul + dst*(1-alpha)`, so a 0.5/255 alpha error perturbs the output by only `dst * 0.5` LSB —
against this family's near-black canvas (dst ≈ 14/255) that is ~0.03 LSB. Below anything visible.

**The platform ALSO dithers these draws.** Compose's `DrawScope` paint is `android.graphics.Paint(7)`
= `ANTI_ALIAS | FILTER_BITMAP | DITHER_FLAG` (`AndroidPaint.android.kt: makeNativePaint`), and Skia
dithers any non-constant shader on RGBA_8888 (`SkPaintPriv::ShouldDither`) — a `RuntimeShader` is
non-constant. Verified in pixels: `AuraSpark`, which has NO app-level dither, still renders an
ordered ±1 LSB lattice in locally-flat regions while its alpha=0 background is exactly `[0]`. So the
app-level term is belt-and-braces, and it only holds while we draw via `ShaderBrush`/`drawRect` in a
`DrawScope` — a future move to a `RenderEffect` or a `graphicsLayer` would silently drop the
platform's half of it.

## Rule 3b — the noise interpolant is quintic (a QUALITY choice, not a bug fix)

`valueNoise` interpolates with `f*f*f*(f*(f*6-15)+10)` (the "improved Perlin" quintic) rather than the
classic `f*f*(3-2f)` smoothstep. Keep it — free, standard, strictly smoother.

**Do NOT cite it as the cause of any on-device artifact.** The cubic Hermite is already C1 (continuous
value AND first derivative); only its SECOND derivative jumps at a lattice boundary. Mach banding
requires a FIRST-derivative discontinuity. Perlin's own reason for the quintic was smooth normals when
noise is consumed in DERIVATIVE space (bump/displacement mapping), which is not what this family does.

## Rule 4 — hue blends by the glow's own intensity, never by angle/position

A 4-stop angular hue wheel with a phase uniform has a periodic bug where the wrong hue dominates the
visible band (confirmed via capture). Every real
capture (9 columns across two live states) reads blue-family only — zero green/clay/violet.

Both shaders derive color from a plain two-stop `mix(colorLight, colorDeep, intensity)`, where
`intensity` is the SAME bounded value already driving alpha — no separate angle/phase uniform left to
drift out of sync, which removes the hue-rotation bug class by construction.
`AuraColors.auroraOverlayBloom`/`auroraWashTop` are the two-stop token lists; do not reintroduce a
3rd/4th stop or an angle-driven mix without new capture evidence. Stop ORDER is measured too: the
bloom's bright bottom edge is the PALE stop, fading through deep (`mix(deep, light, glow)`, Rule 6).

## Rule 4b — aurora hue field: bounded aperiodic noise-drift, NOT the outlawed angular wheel

The current shader re-introduces multiple hues WITHOUT re-introducing Rule 4's bug class. The distinction is
load-bearing and must survive any future edit:

- The two-stop pair drifts through THREE families (A = `auroraOverlayLiveBloom` blue →
  B = `auroraOverlayVioletBloom` → C = `auroraOverlayEmberBloom`) sampled by a slow, **bounded,
  APERIODIC value-noise field** over (x, y, iTime×rate). Noise is bounded in [0,1] and never rotates
  — no angle, no phase uniform, nothing that can wrap around and let a hue periodically dominate.
  Violet sits BETWEEN blue and ember so the A→B→C path never crosses muddy gray.
- **INTENSITY still selects light-vs-deep within the sampled family and still drives alpha** — Rule
  4's alpha/hue coupling is intact. The drift picks WHICH family; `glow` picks where in that family's
  light↔deep ramp you are (`mix(hueDeep, hueLight, glow)`, Rule 6 order preserved). No 3rd stop in
  any single `mix`, no angle term.
- **`iHueDrift == 0` is the chat byte-path.** At 0 the family blend collapses to the pure A pair AND
  the hue-noise sample is BRANCH-SKIPPED (`if (iHueDrift > 0.0)`) — chat renders the two-stop navy,
  single-hue, zero extra ALU. `ChatScreen` passes no `hueDriftAmount`; only the overlay (and the debug
  showcase) pass 1f. Verify with the chat bottom-edge no-drift pixel check (single-hue navy, b>g>r
  everywhere) on any change here.
- **Reduced motion keeps the FIELD, drops only the travel.** `iHueDrift` is NOT reduced-motion-gated
  — the drift SPEEDS (`iTime × iWaveSpeedHz`) are already 0 there, so the field freezes into a STATIC
  multi-hue frame (device-verified: two captures 3s apart, 0/154440 glow pixels changed). Same "field
  stays, motion stops" shape as the breathe/wave.
- The family blend is written branch-free (`mix(mix(A,B,segAB), C, segBC)` with
  `segAB = clamp(hueT·2,0,1)`, `segBC = clamp((hueT−0.5)·2,0,1)`) rather than a ternary on a float3 —
  the ternary-with-vector form risks a runtime-compiler failure that renders the whole shader black;
  the branch-free composition is bit-for-bit identical and compiles on SwiftShader.
- Verify hues across SPACE and TIME — a single frame can sit in a locally-monochrome region of the
  aperiodic field. Device-verified multi-hue: one steady Listening frame read violet center
  (`130,101,183`), ember right rail (`104,68,86`, r>b), blue left (`60,47,91`) simultaneously, R/B
  ratio climbing 0.66→1.21 across x.

## Rule 5 — state-dependent reach is measured per state, not one shared constant

`EdgeGlowState.Listening` (idle-listening) reads WIDE: measured faint-but-real reach into the bottom
corners, visible to ~143dp+ at the screen edges. `EdgeGlowState.Thinking` (streaming/generating)
contracts to hug the pill with NO corner reach. Both still peak at the true bottom edge (Rule 2 is
state-independent) — only `decayLength` (`EdgeGlowUniformMath.decayDepthDp`) and how hard corners get
suppressed (`centerWeightStrength`) differ. A contracted decay length ALONE cannot darken corners,
because the bottom corners sit on the SAME `distFromBottom=0` row as the peak — Thinking therefore
also needs a much stronger `centerWeight`.

`AuraColors.auroraOverlayBloomDecayDepth` (45dp) is the ONE measured token (Listening's wide reach);
Thinking's contraction is a local behavioral ratio (`THINKING_DECAY_FRACTION = 0.22f`) against it.
`EdgeGlowState.Resting` (overlay-scoped) adds a THIRD local ratio pair against the same token:
`OVERLAY_RESTING_DECAY_FRACTION` (0.45) between Listening's full reach and Thinking's hug, and
`OVERLAY_RESTING_INTENSITY` (0.55) dimming it — both private constants in `EdgeGlowUniformMath`,
reusing Listening's `centerWeightStrength` since Resting is presence, not corner-suppression.

## Rule 6 — a reach measurement is NOT a decay length: back-solve it

Reading "visible to ~143dp at the edges" as if reach were the exponential's length parameter set
`auroraOverlayBloomDecayDepth = 45dp`. An exponential with L=45dp is at 4% by 143dp — combined with a
halved peak alpha the entire effect rendered INVISIBLE behind the overlay's opaque bottom chrome
("aurora renders black"; a channel-encoded diagnostic shader confirmed every term healthy, i.e.
tuning, not code). **The law:** given a scanline sample at distance d still showing fraction r of
peak, `L = -d / ln(r)` (ref2 edge column: r≈0.36 at 143dp → L≈139dp).

Two measured corollaries: peak alpha back-solves near-opaque at the bottom edge (#6D85B9 over #2A2A2E
scrim), not a 0.5 "glow, never a stripe" cap — stripe-avoidance comes from the continuous falloff,
not from starving the peak; and the hue order is PALE at the bright bottom edge fading through deep
(`mix(deep, light, glow)`), not the reverse.

When a shader "renders black/nothing" with zero compile errors, suspect tuning-vs-occlusion before
pipeline: the channel-encoding diagnostic (R/G/B = one suspect term each, alpha=1) answers it in one
build.

## Run-state liveness — solid at rest, ONE glow layer (ChatScreen)

DESIGN.md ("always tell running from dead"):

- **Solid-resting law.** The app's native resting state is a SOLID background: NO aura, transparent
  top bar. "Running vs dead" is legible precisely because the resting state paints nothing. Do not add
  an ambient/decorative layer that survives into rest. An always-on `Resting` top wash in chat shipped
  and was reverted (DESIGN.md); its constants (`RESTING_INTENSITY_FRACTION`,
  `ACTIVE_DRIFT_BOOST`) are DELETED and `AuroraWashTop` now renders ONLY in the debug
  `LivenessShowcase`.
- **ChatScreen sole-owner law.** In chat there is exactly ONE liveness layer: `ChatScreen`'s bottom
  `AuroraEdgeGlow`, tuned via caller-side `IN_APP_GLOW_*` reach/center-weight/alpha overrides.
  `ChatSurface` renders NO aura of its own (it only derives a `washActive` boolean to style
  `ChatTranscript`'s `overWash`). Nothing paints behind the top bar.
- **Invocation window.** On a fresh chat-screen entry the glow ambient-breathes as
  `EdgeGlowState.Listening(0f)` for `AMBIENT_INVOCATION_WINDOW_MS`, then flips to
  `EdgeGlowState.Hidden`. A live run overrides this with `EdgeGlowState.Thinking` for the whole
  `Sending`/`Streaming` phase.
- **Run-end ease-off (photosensitivity).** When a run ends the glow must NOT snap from `Thinking` to
  black — an abrupt on→off luminance change reads as a flash. `ChatScreen` passes
  `AuroraEdgeGlow(dismissFadeMs = AuraMotion.edgeRestFadeMs)` (≥3s) and the composable eases the alpha
  envelope (`iVisible`) to 0 over that duration on a LINEAR curve (constant rate, no fast segment). It
  CANNOT fade by handing the shader `Hidden`'s own uniforms: `intensity(Hidden) = 0f` zeroes alpha
  instantly and `Hidden`'s WIDE reach would pop the bloom. So while the fade is in flight the
  composable HOLDS the last active state's whole frame (reach, intensity, wave phase AND speed, via a
  `renderState`/`renderSpeed` latch) and ramps only `iVisible`. **Holding the wave SPEED is
  load-bearing** — the caller drops `speedScale` at the flip, and a rate change while `iTime` is large
  would jump the drift. The overlay passes its own short `dismissFadeMs` (`OVERLAY_GLOW_DISMISS_MS`)
  through the same caller knob, never a fork. `reducedMotion` still `snap()`s (a single monotonic
  fade-to-black is not a flash either way).
- **`speedScale` is rate-only (phase-continuous).** It multiplies the wave-drift RATE
  (`iWaveSpeedHz`) and nothing else — never the accumulated phase. It cannot jump the wave: the
  shader's phase input `iTime` is `EdgeGlowTransitionTracker`'s `elapsedInState`, which RESETS to 0 at
  every state transition, and a state transition is the ONLY moment `speedScale` can change. At
  `iTime == 0` the term `iTime * iWaveSpeedHz` is 0 for any scale. This is DESIGN.md's
  "speed changes that jump" trap avoided by construction — scale the rate, never the phase.
- **Top-fade edge-window law.** A large caller `reachScale` (the in-app 5.5×) leaves the vertical
  exponential clearly nonzero at the TOP draw bound, which hard-stops into a thin horizontal seam
  where the surface clips. The shader feathers the bloom to EXACTLY 0 over the top `TOP_FADE_FRACTION`
  (0.18) of the surface height (`smoothstep(0, res.y * iTopFadeFraction, fragCoord.y)`). Same
  edge-window law as the orb halo (`1 - smoothstep(0.44, 0.5, dist)`): **alpha must reach 0 before any
  draw bound, always** (DESIGN.md). Monotonic smoothstep, no plateau; the mandatory dither still
  runs. No-op for small-reach bottom-anchored callers (the overlay, `reachScale` 1).

## Perimeter bloom — the invocation ignite → settle envelope

The overlay's invocation moment is a one-shot PERIMETER bloom riding `AuroraEdgeGlow`'s entrance and
decaying into the bottom-only live glow, without a second shader or a radial mask.

- **Edge-anchored terms PEAK at the draw bounds — that is not a seam.** The perimeter is three
  edge-anchored exponentials in `AURORA_EDGE_SHADER_SRC` — a per-column `sideGlow` from the L/R edges,
  a `bottomBias` (`mix(0.25, 1.0, heightFrac²)`) so the light reads as EMANATING from the pill, and a
  faint `topGlow`. Rule 1 holds (same construction as the bottom term, rotated 90°). The top-fade
  window scopes to the BOTTOM-ANCHORED `vGlow` term ONLY, NOT the perimeter: forbids a *clipped
  mid-falloff*; an edge-anchored term that PEAKS at the bound has no beyond-the-bound region to step
  against. Device-verified: the top draw bound sampled 0.0 luminance in 14/16 overlay frames, a faint
  transient corner tint (~9–14/255) only during the single peak-bloom frame, pure 0 in every settled
  frame. The perimeter's inner boundary undulates because `sideDecay` derives from `decayLength`,
  which the existing `valueNoise` wave already modulates — zero new noise terms.
- **The bloom envelope is CALLER-owned.** `AuroraEdgeGlow(perimeterBloom: State<Float>? = null)` is a
  0..1 value the CALLER animates — the composable only turns it into pixels, the same externally-driven
  pattern as `EdgeGlowState.Igniting`'s progress and the orb's `rmsDb`.
  `ui/overlay/OverlayAnimations.kt`'s `rememberPerimeterBloom` owns the timeline: snap to 1 on leaving Idle, hold
  through the 450ms ignite (`AuraMotion.edgeSweepMs`), exhale to 0 over `AuraMotion.bloomSettleMs`
  (950ms, FastOutSlowIn), reset on Idle. Read in the DRAW phase only so the per-frame value never
  subscribes the composable to recompose. `null`/0 = no bloom; `ChatScreen` never passes it, so chat
  is byte-identical (the shader collapses to `glow = vGlow` when `iPerimeterBloom == 0`).
- **The ignition color pair lerp is CPU-side (Rule 4 intact).** At bloom=1 the pair lerps to
  `AuraColors.auroraIgnitionBloom`; at 0 back to the caller's own `colors`. The lerp happens in Kotlin
  before the uniform set — the shader still sees a plain two-stop `mix(deep, light, glow)`. Caution:
  Compose's `Color.lerp` round-trips through Oklab, so `t=0` byte-identity is value-specific (~1% of
  arbitrary color pairs drift 1 LSB) — don't cite byte-identity as an invariant after retuning either
  pair (imperceptible under the mandatory dither).
- **Reduced motion: belt AND suspenders.** `rememberPerimeterBloom` never raises the envelope when
  `reducedMotion`, AND `AuroraEdgeGlow` forces `bloom = 0f` in the draw phase regardless of what a
  caller passes. Device-verified with `animator_duration_scale 0`: no bloom burst, static bottom glow,
  top edge 0.0 in every frame.

`EdgeGlowState.Resting` (OVERLAY-scoped) is what the bloom settles toward when the first turn is
done (`Streaming.done`) but the overlay is still on screen — a low, STATIC (no breathe — oscillation
would read as still "doing something") bottom pool between Thinking's hug and Listening's wide reach.
**NEVER used by the chat surface**: `ChatScreen` maps nothing to `Resting`, and 's solid-resting
law is untouched — an on-screen overlay is a live surface; the chat surface's rest is not.

## Fluid edge-lit perimeter — persistent floor + 2-octave + liquid level

Three additive mechanisms, all inside the existing rules:

- **Persistent perimeter floor.** `perimeter × max(iPerimeterBloom, iPerimeterFloor)` — a live bloom
  still wins (it's ≥ any floor), but after it settles the floor keeps a faint side/top presence so the
  state reads edge-lit (previously the perimeter collapsed to zero once the bloom settled, leaving the
  live states bottom-only). `iPerimeterFloor = EdgeGlowUniformMath.perimeterFloor(state) ×
  perimeterPresence`, an exhaustive per-state `when` (Listening/Igniting 0.35 · Resting 0.30 ·
  Thinking 0.15 · Hidden 0 — Thinking lowest because its contracted, corner-suppressed hug is a
  designed state, not an edge-lit one). `perimeterPresence` is the caller knob: overlay 1f, **chat
  default 0f** → the perimeter block zeroes and chat stays bottom-only. NOT reduced-motion-gated (a
  static edge presence, not travel). Device-verified: steady Listening side rails lit ~66–128 (blue
  left, ember right) where they were ~0 before; chat side points stayed dim ~19–25 navy.
- **2-octave reach wave.** A coarse swell (0.65) + a finer, faster octave (0.35, 2.3× spatial freq /
  1.9× speed) so the boundary reads as liquid, not one sine-like undulation. Both are `iTime × rate`
  (phase-continuous). This is a SHARED term — it modulates `decayLength` for every caller including
  chat; the chat byte-path guard is specifically the hue+perimeter branch-skip, not the wave.
- **Per-row liquid level.** A second value-noise wave over `y`+time modulates the side reach
  (`sideDecay`) so the edge light rises and falls ALONG the edges, scaled by the same `iWaveAmplitude`
  token (0 under reduced motion → static). Phase-continuous.

All three are additive `valueNoise` samples (+3/px worst case incl. the Rule-4b hue field, branch-
skipped at `iHueDrift 0`) — accepted on SwiftShader for geometry checks, never FPS.

## The border profile — `perimeterBias`, and why it is ONE knob

The device-control overlay is the surface whose whole job is to say "an agent is driving your phone
right now". At the bottom-anchored balance every other caller uses it read as a **bottom wash**:
`glow = clamp(vGlow + perimeter, 0, 1)` is a SUM, so at Listening's wide reach the bottom-anchored
term saturates the lower third *before the perimeter contributes anything there*, and the side rails
are what is left over rather than the subject. Numerically, at `perimeterBias = 0` (1080×2400 @2.75,
noise held at 0): bottom-centre **1.000**, side rails **0.23**, top **0.20**.

`AuroraEdgeGlow(perimeterBias: Float = 0f)` shifts that balance. At 1: bottom-centre **0.996**, side
rails **0.992**, top **0.993**, screen-centre **0.00046** (0.12 of an 8-bit LSB — quantizes to 0) —
an even border with a saturated corner join, and the haze gone.

**Seven effects, one knob, and splitting them is the trap.** Each alone re-opens the imbalance:
raising the floor without levelling the bottom just makes the bottom the subject again; damping the
bottom without raising the floor dims the surface; either without contracting the reach leaves a
haze with brighter edges.

| Effect | Where | At bias 1 |
|---|---|---|
| per-state perimeter floor gained | CPU, `perimeterGain` | ×2.857 → Listening 0.35 → **1.00** |
| bottom-anchored term held level with the rails | shader `iBottomWeight` | ×1.00 (see parity below) |
| reach contracted | CPU, `reachFraction` | ×0.28 → 139dp → 39dp; **also the rail thickness**, since `sideDecay` derives from `decayLength` |
| horizontal center-weight flattened | CPU, `centerWeight` | ×0 — a bloom is brightest under the pill, a border's bottom edge is EVEN |
| `bottomBias` + top term to four-edge parity | shader `iPerimeterBias` | the two terms that make the light read as EMANATING from a composer pill this surface does not have |
| `peakAlpha` spends its last tenth | shader `iPerimeterBias` | 0.9 → **1.0** |
| hue field's SPATIAL frequency raised | shader `iPerimeterBias` | ×2.2 — ~1 noise cell spans the surface at bias 0 (one family per frame), ~2–3 at bias 1, so the families play ALONG each rail |

### Parity is ONE number, and it is the border's brightness

`BORDER_PARITY_LEVEL` is the level both the bottom edge and the rails peak at, as a fraction of the
shader's `glow` term; `BORDER_PERIMETER_GAIN` is *derived* from it
(`BORDER_PARITY_LEVEL / LISTENING_PERIMETER_FLOOR`) and `bottomWeight` reads it directly. That
one-source shape is what makes the border even by construction — two independently-tuned numbers
drift apart the first time either moves, and a bottom brighter than the sides is the bottom wash
again with extra steps. The float32 round-trip `0.35f * (1f / 0.35f)` is exactly `1.0`, so the
identity holds bit-exactly and the test asserts exact equality rather than a tolerance.

**Parity 1.0 is the ceiling, and the first round's 0.70 reservation was refuted, not retuned.** It
claimed the rails needed "headroom against the bottom term where the two meet at the corners". The
corner join was *already* saturated at 0.70: on a 1080×2400 @2.75 frame with noise at 0, the
bottom-left join summed `vGlow 0.697 + perimeter 0.695 = 1.391` and clamped. The corner saturates at
any parity above 0.5, so the reserve bought no gradient where it claimed to and cost 30% of the
surface's luminance everywhere else — which is what a real device read as "practically invisible".
Two accepted consequences, both measured off the same probe:

- the saturated fillet hugging the two bottom corners grows ~1.8k px → ~11k px (0.07% → 0.43% of
  the frame). It is a sliver, not a block: it reaches 520px up only in the outermost column and is
  under 2px deep by 256px in. The hue-drift field still varies across it — only the light-vs-deep
  ramp *within* a family is flat there;
- at the very edge the Listening breathe clips on its up-swing (glow 0.99 × intensity 1.2 clamps),
  so the outer ~11px of each rail breathes downward only. Past that the full swing is expressed.

**Raise the parity, never `iIntensity`** — they are the same scalar on the alpha and are NOT the
same on the colour. `glow` also feeds `mix(hueDeep, hueLight, glow)`, so raising parity moves the
border toward the PALE stop exactly as Rule 4/Rule 6 intend, while an intensity boost leaves the
colour mid-ramp and brightens only the alpha: a brighter, muddier border. Intensity also carries the
breathe, so scaling it scales the swing — the wrong direction for photosensitivity — and any boost
past the clamp manufactures the flat plateau Rule 2 forbids, where parity keeps the exponential's
shape and clamps only at the edge itself.

**What is NOT available: the host window's obscuring-alpha cap** (`ui/control`, 0.8). Above it
Android 12+ revokes touch pass-through and the window swallows every touch on screen, the user's and
the agent's injected taps alike, with nothing reporting a problem. It is a WINDOW property, so
per-pixel shader alpha does not enter that decision — which is why `peakAlpha` may go to 1.0 while
that number does not move. With parity and `peakAlpha` both at their ceilings, the composited rail
peak is `1.0 × 1.0 × 0.8 = 0.80`, up from `0.70 × 0.9 × 0.8 = 0.50`. **There is no further headroom
in this chain**; the only levers left are the colour pair (`AuraColors`, shared with the assist
overlay) and the reach, and the reach is what stopped it being a haze.

**Byte-identity is by CONSTRUCTION, not by tuning.** Every one of the seven is an exact identity at
bias 0: `mix(x, y, 0.0)` is `x*(1-0) + y*0` = `x` exactly (and `x + 0*(y-x)`, the other lowering a
compiler may choose, is equally exact), and a `* 1.0` is exact in IEEE. So chat
(`perimeterBias` defaulted, `hueDriftAmount` 0, `perimeterPresence` 0) and the assist overlay
(defaulted) render the same bytes as before. `perimeterGain` is applied AFTER `perimeterPresence`,
so a caller at presence 0 stays at exactly 0 for any bias.

**It does NOT touch the drift rate.** `speedScale` stays the single rate source with its own
phase-continuity contract; a second rate source would need that argument re-derived. Reduced motion
is untouched for the same reason the floor and the hue field are: a border is static PRESENCE, not
travel — only the drift speeds are 0 there, so reduced motion keeps an even, multi-hue, frozen
border rather than falling back to a bottom wash.

Known asymmetry, accepted: the bottom band stays ~1.7× thicker than the rails, because `sideDecay`
carries a 0.6 factor the bottom term does not. Peaks match; thickness does not. The bottom being the
heaviest edge is consistent with the rest of this family. Raising the parity does not change the
RATIO, only the absolute widths — taking "still above 5% composited alpha" as the visible band, the
rails go 148px → 178px (54dp → 65dp) and the bottom 247px → 296px (90dp → 108dp).

## The rise bound — `riseFraction`, and why the border needed one

The border profile's side rails run the FULL height of the surface by construction: at
`perimeterBias = 1` the `bottomBias` mix flattens to 1.0, so a rail is as bright at the top row as at
the bottom. On a tall handheld that reads as a frame. On a short, wide 16:9 television it reads as a
wash over most of the screen — reported from a physical panel as the aura "spanning from the bottom
to the top" and eating more than half the height.

`AuroraEdgeGlow(riseFraction: Float = 0f)` bounds how far up the WHOLE composite reaches — rails, top
term and bottom term alike, applied to `glow` after the sum. **It is not a retune of the border**, and
that distinction is the point: the border is what makes the surface legible at every edge, and
lowering the parity or the floor to shrink it would take the legibility with it.

- **A fraction, never a dp.** The complaint is about the PROPORTION of the screen the surface eats,
  and a dp says something different on every panel.
- **Branch-skipped at 0** (`if (iRiseFraction > 0.0)`), the same idiom as `iHueDrift` — so every
  existing caller is byte-identical by CONSTRUCTION rather than by tuning, the same standard the
  border profile itself is held to.
- **It feathers over the upper 45% of the allowed rise**, reaching exactly 0 well below the bound.
  This is the edge-window law applied to the terms the top fade deliberately leaves alone:
  `iTopFadeFraction` scopes to `vGlow` only, because the perimeter terms PEAK at the draw bounds and
  have no mid-falloff for a clip edge to slice. Once a rise bound is on they no longer reach a bound
  — so they DO have a mid-falloff, and it has to be feathered or it hard-stops into a horizontal seam
  straight across the screen.
- The shader-free API 30-32 fallback honours it too, as a ceiling on the band height. That path has
  no perimeter and was never the surface this was written for, but a caller asking for a bounded rise
  must not get an unbounded band merely because the device is too old for the shader.

The value is a `DeviceShape` member (`controlAuraRiseFraction`), so which shapes need a bound stays
one answer rather than a literal per call site. **Unmeasured:** 0.4 is the 60% cut that was asked
for, not a number read off a capture — nothing here has run on a television.

## Flow rate — derive it from the breathe, never pick it

The device-control surface passes `speedScale = AuraMotion.deviceControlFlowScale` (2.3) because at
the ambient pace it read as "just slightly breathing". **The value is derived, and a future retune
must re-derive rather than nudge:** the fastest drift term in the shader is the reach wave's fine
octave, whose noise argument advances at `WAVE_DRIFT_HZ × 1.9` ≈ 0.067 value-changes/s at a fixed
pixel; the fastest periodic term this family already ships — and the one the design language already
calls calm — is the `listeningBreathePeriodMs` breathe at ≈ 0.154 Hz. 2.3 is their ratio, so the
fastest drift term lands exactly ON the breathe cadence and **nothing on the surface runs faster
than a rate already accepted**. That sits ~20× under the 3 Hz photosensitivity flash threshold, and
what it modulates is a smooth gradient's geometry (±22% of the decay length), never a full-area
luminance step. Reduced motion is unaffected — the base speed is already 0, and 0 × 2.3 is 0.

## Tuning-constant provenance — measured token vs. behavioral ratio

Every `AuraColors`/`AuraSpacing` value consumed here must be traceable to an actual capture
(pixel-sampled hex, measured dp/px reach) — that's a MEASURED token. A value that only exists to
relate two measured tokens (`THINKING_DECAY_FRACTION`, `THINKING_INTENSITY`, `WAVE_AMPLITUDE`) is a
BEHAVIORAL TUNING CONSTANT — it lives as a private constant in `EdgeGlowUniformMath`/local to the
shader file, never promoted to `AuraColors`/`AuraSpacing`, and its KDoc must say what it's a
ratio/multiplier OF. There is no third category; a value fitting neither is not ready to land.

## Three time-based traps (any shader/animation work here)

- **The frame clock is milliseconds since BOOT — never narrow it to Float raw.**
  `withInfiniteAnimationFrameMillis` yields uptime, not session time. On a phone up 5 days that is
  ~4.3e8 ms, in a binade where the float32 ULP is **32 ms** — so `timeSeconds` only advances every
  OTHER frame and the wave JUDDERS in ~1.1px jumps instead of drifting ("not fluid"); by ~11 days the
  ULP doubles again into 4-frame stalls. It degrades with UPTIME, which is why redroid never shows it
  (containers live for minutes) and only a real phone does. `rememberShaderTimeSeconds()` anchors to the
  first observed frame **in `Long`, where the subtraction is exact**, and narrows only the small
  difference; the origin sits in a `remember` OUTSIDE the `LaunchedEffect(isVisible)` so a lifecycle
  pause/resume RESUMES the phase. Safe for every consumer today because they are all delta-based or
  sine-of-t (`ShaderRotationAccumulator.advance` integrates `dt`, `EdgeGlowTransitionTracker` and
  `Orb()`'s `stateEnteredAt` subtract an origin, the wash takes `% 1000f`) — verify that before adding a
  consumer that wants an absolute clock, because none exists.
- **`LaunchedEffect(transitionKey)` ordering race.** A transition-tracking effect and
  `rememberShaderTimeSeconds()`'s own internal `LaunchedEffect` both launch on the same composition
  pass with no guaranteed relative order — the tracker can read `timeSeconds` before the frame clock's
  first tick, capturing 0 instead of the true time and silently re-breaking "state X at the transition
  moment." Fix: track the transition in the DRAW phase — `AuroraEdgeGlow.kt`'s
  `EdgeGlowTransitionTracker`, read exclusively inside `onDrawBehind`, which strictly follows that
  frame's `timeSeconds` update. `Orb.kt`'s `OrbUniformMath` error-pulse envelope has the IDENTICAL
  latent pattern, still unfixed (low-visibility — it only shifts the pulse decay's starting phase);
  fix it the same way if ever touched.
- **Verify animated properties across TIME, not just at t=0.** A rotating phase riding on static
  geometry that checks out analytically can still periodically let the wrong hue own the visible band
  — a t=0 check or a single screenshot cannot catch it. Sample across several full cycles of any
  oscillating uniform in `LivenessShowcase` before calling a shader change verified.

## Verification host

`LivenessShowcase` (debug-only, `ui/aurora/LivenessShowcase.kt`) is the one place all
`AuroraState`/`EdgeGlowState` values are exercised side-by-side without a live backend; its Wash page
exercises all four `AuroraState` values including `Resting`. The Edge-glow page's **Profile** button
toggles the border profile in place (`perimeterBias` + `deviceControlFlowScale` together, the two
arguments that are the whole difference between the device-control surface and the assist overlay) —
toggling in place is what makes "border, not bottom wash" checkable instead of asserted. Any new state or shader parameter needs a
page/variant here before it can be called verified — a single dev-build screenshot is not enough (see
the verify-across-time trap). redroid's software GPU (SwiftShader) is fine for judging
geometry/crispness; never judge FPS on it.
