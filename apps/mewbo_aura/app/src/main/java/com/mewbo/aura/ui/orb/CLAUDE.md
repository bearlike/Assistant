> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Orb + Shared Shader Primitives — ui/orb/

Scope: `ui/orb/` — the brand orb (`Orb`), the `AuraSpark` brand mark, and the AGSL primitives the WHOLE
shader family shares (`GlslNoise`, `ClayFlowerSdf`, `ShaderFrameClock`). Shader postmortems +
falloff/dither/hue laws for the aurora family live in [`ui/aurora/CLAUDE.md`](../aurora/CLAUDE.md); this
file owns the orb itself and the primitives.

## The orb IS the brand mark

An AGSL **polar SDF of the console's 8-lobed clay flower** (`ClayFlowerSdf.petalShape`, r(θ) FFT-fit
from the real SVG path), NOT a generic radial-gradient blob — v1 shipped soft gaussian blobs and was
rejected as "blurry"; don't regress toward decorative blur. Non-negotiables:

- **Crisp edges + edge-window alpha-0-before-bounds.** The silhouette is AA'd with ~1.5px smoothstep
  windows; halo/ring terms carry an explicit `1 - smoothstep(0.44, 0.5, dist)` window so alpha is
  EXACTLY 0 before the draw bounds — an `exp(-90·d·d)` falloff alone shipped a visible square edge once
  (DESIGN.md §7.9).
- **All per-state uniform derivation is in `OrbUniformMath`'s exhaustive `when(state)`** (including
  `pulseEnvelope`) — a new `OrbState` fails compilation, never silently loses behavior. No `if (state is
  X)` at call sites. Transition-driven scale via `graphicsLayer{}` read in the DRAW phase (never
  `Modifier.scale()`, never recomposing `Orb()`). `transitionKey` discriminates state KIND (ignoring
  `Listening.rmsDb`) so `LaunchedEffect(transitionKey)` doesn't retrigger on every RMS tick.
- Reduced motion reuses the SAME crisp shader frozen (+ an opacity breathe), never a soft fallback.
- `Orb.kt`'s error-pulse envelope carries the same latent `LaunchedEffect`-ordering race documented in
  the aurora file (low-visibility, unfixed) — fix it the same way (draw-phase tracking) if ever touched.

## Shared primitives (consumed by the aurora family too)

- **`GlslNoise`** — `hash21`, `valueNoise` (quintic), `ign` (interleaved gradient noise), and
  **`ditherPremul`, the ONE canonical dither** (DESIGN.md §7.17; consumed by orb, spark, `AuroraEdgeGlow`,
  `AuroraWashTop`). Traps encoded here: `hash21` must NOT be fed raw `fragCoord` (`fract` degenerates to
  ~8 distinct values at large y → use `ign`); keep dither/coord math in `float` (AGSL `half` is fp16,
  can't represent `fragCoord.y * 456.21`); never time-offset the dither (a TAA trick that crawls without
  temporal accumulation). `ditherPremul` takes PREMULTIPLIED rgb and clamps to `[0, alpha]` at ±0.5 LSB —
  clamping to alpha (not 1.0) is what keeps the result a valid premultiplied colour.
- **`ClayFlowerSdf`** — the flower geometry (`petalShape`/`flowerShapeAlpha`, `FLOWER_SCALE`/`HOLE_RATIO`
  Kotlin consts), consumed by `Orb`, `AuraSpark`, and `AuroraEdgeGlow` rather than re-derived.
- **`ShaderFrameClock`** — `rememberShaderTimeSeconds()` (lifecycle-gated; the origin sits in a `remember`
  OUTSIDE the `LaunchedEffect` so a pause RESUMES phase) and `ShaderRotationAccumulator`. The
  boot-ms-into-Float judder (DESIGN.md §7.18) is fixed here: anchor to the first frame in `Long` where
  the subtraction is exact, narrow only the small difference. It degrades with UPTIME — redroid (minutes)
  never shows it, a real phone always will.

## `AuraSpark` / `SparkState`

`AuraSpark` reuses the SAME silhouette but paints a conic 4-stop brand gradient swept around the RAW
screen angle (hue drifts across lobes vs spinning rigidly); NO halo/ring/error terms (orb-only).
`SparkState` has exactly TWO variants — `Shimmer` (48dp greeting) and `Thinking` (in-chat thinking
indicator). Gradient stops come from `AuraColors.sparkGradient`, not `LocalAssistantExtras`.
`SparkUniformMath` mirrors `OrbUniformMath`'s exhaustive-when discipline. Verification host: debug-only
`LivenessShowcase` ([`ui/aurora/CLAUDE.md`](../aurora/CLAUDE.md)); redroid's SwiftShader judges
geometry/crispness, never FPS. (`OrbShowcase.kt`'s KDoc still names the deleted `OrbShowcaseActivity` —
stale prose, not a code issue.)
