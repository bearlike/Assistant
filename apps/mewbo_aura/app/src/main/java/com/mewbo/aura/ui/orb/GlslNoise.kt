package com.mewbo.aura.ui.orb

/**
 * Shared AGSL noise primitives (2D hash + value noise + the ONE canonical dither), extracted from
 * the orb v2 shader so every RuntimeShader in the app (orb, spark, aurora edge glow, aurora wash)
 * concatenates the identical math instead of hand-copying it per shader — a pixel-for-pixel drift
 * between copies is the kind of bug that only shows up as "this one looks slightly different" in a
 * screenshot diff, and it DID: the two aurora shaders each hand-rolled their own dither line with
 * two DIFFERENT formulas (a hash at 2/255 vs. a spatially-correlated valueNoise at 3/255 —
 * correlated noise is not a dither at all) until [ditherPremul] below made it one primitive.
 */
internal object GlslNoise {
    const val GLSL_CORE = """
// Fed INTEGER lattice points by valueNoise (small magnitudes), where fract() is exact. Do NOT reuse
// it on raw fragCoord: at the bottom of a 1080x2400 display y*456.21 is ~1.09e6, where the float32
// ULP is 0.125, so fract() retains ~3 bits — ~8 distinct values. That degeneracy is exactly why the
// dither below uses ign(), not this. (Lattice use is fine and stays.)
float hash21(float2 p) {
    p = fract(p * float2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

float valueNoise(float2 p) {
    float2 i = floor(p);
    float2 f = fract(p);
    float a = hash21(i);
    float b = hash21(i + float2(1.0, 0.0));
    float c = hash21(i + float2(0.0, 1.0));
    float d = hash21(i + float2(1.0, 1.0));
    // Quintic ("improved Perlin") interpolant rather than the classic cubic Hermite f*f*(3-2f).
    // This is a QUALITY improvement, not a bug fix — do not cite it as the cause of any on-device
    // artifact. The cubic is already C1 (continuous value AND first derivative); only its second
    // derivative jumps at a lattice boundary, and Mach banding requires a FIRST-derivative
    // discontinuity, so a C2-only kink is not a known source of a visible line. Perlin's own reason
    // for the quintic was smooth normals when noise is used in DERIVATIVE space (bump/displacement),
    // which is not what we do. Kept because it is free, standard, and strictly smoother.
    float2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// Interleaved gradient noise (Jimenez). The dot product keeps fract()'s argument SMALL (<~100 for
// any real screen), so precision holds at ANY fragCoord — unlike hash21's `fract(p * big)` above.
// Stays in `float` (highp) deliberately: AGSL `half` is fp16, whose max finite value is 65504 and
// which therefore cannot even REPRESENT fragCoord.y * 456.21. Purely spatial — never offset it by
// time. Time-offsetting IGN is a TAA trick; with no temporal accumulation to average it, the grain
// would visibly crawl.
float ign(float2 p) {
    return fract(52.9829189 * fract(dot(p, float2(0.06711056, 0.00583715))));
}

// The ONE canonical dither for every dark-gradient shader (ui/aurora/CLAUDE.md Rule 3).
//
// It takes the PREMULTIPLIED rgb, because that is the only space where a dither does anything. Skia
// surfaces are premultiplied: what reaches the framebuffer is `rgb * alpha`, so a dither added to
// the UNPREMULTIPLIED colour gets scaled by alpha on the way out — its effective amplitude becomes
// `alpha` LSB, not 1 LSB. It would be strongest at the peak (where the ramp is steep and banding is
// invisible) and vanish in the faint reaches (where the ramp is flattest, the contour bands widest,
// and banding is MOST visible) — i.e. exactly backwards. That is how a dither can be present,
// reachable, correctly shaped, and still fail on real glass.
//
// Amplitude is +/-0.5 LSB, matching Skia's own production dither (src/gpu/DitherUtils.cpp:
// DitherRangeForConfig -> 1/255 for RGBA_8888). The "uniform dither needs 2x amplitude" result
// applies to WHITE noise; IGN is ordered/low-discrepancy, for which +/-0.5 LSB fully resolves a ramp.
//
// The clamp is to `alpha`, NOT to 1.0 — clamping to alpha is what keeps the result a VALID
// premultiplied colour (rgb <= a). Same shape as Skia's own dither effect.
float3 ditherPremul(float3 premul, float alpha, float2 fragCoord) {
    float3 dithered = premul + ((1.0 / 255.0) * ign(fragCoord) - (0.5 / 255.0));
    return clamp(dithered, 0.0, alpha);
}
"""
}
