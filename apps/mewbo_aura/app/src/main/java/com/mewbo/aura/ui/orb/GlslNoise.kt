package com.mewbo.aura.ui.orb

/**
 * Shared AGSL noise primitives (2D hash + value noise), extracted verbatim from the orb v2 shader
 * so every RuntimeShader in the app (orb, spark, aurora wash) that needs cheap dithering or
 * organic shimmer concatenates the identical math instead of hand-copying it per shader — a
 * pixel-for-pixel drift between copies would be the kind of bug that only shows up as "this one
 * looks slightly different" in a screenshot diff.
 */
internal object GlslNoise {
    const val GLSL_CORE = """
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
    float2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}
"""
}
