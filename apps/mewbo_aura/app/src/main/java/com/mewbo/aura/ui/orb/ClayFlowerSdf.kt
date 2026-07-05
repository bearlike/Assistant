package com.mewbo.aura.ui.orb

/**
 * Shared AGSL geometry for the Mewbo clay-flower brand mark: a polar SDF of the console's 8-lobed
 * silhouette, r(theta) FFT-fit against the true SVG path (see [GLSL_CORE]'s doc comment inside the
 * shader text), with a circular hole subtracted and crisp ~1.5px smoothstep edges — never a soft
 * gaussian falloff (that read as "blurry, low quality" in v1's review round).
 *
 * [Orb] and `AuraSpark` both concatenate [GLSL_CORE] into their own shader programs so the
 * silhouette geometry can never drift between the two renderings; only the surrounding color/energy
 * math (sweep + halo + ring for the orb, conic brand gradient for the spark) differs per shader.
 */
internal object ClayFlowerSdf {
    /** flowerScale/holeRatio as Kotlin constants so shaders that need the raw numbers again outside
     * [flowerShapeAlpha] (the orb's halo/ring falloff, keyed off the same silhouette radius) share
     * this one source of truth instead of re-typing the literals. */
    const val FLOWER_SCALE = 0.35f

    // fit hole radius (60) / fit mean outer radius (224.94), sampled from the same SVG pass as
    // the petalShape coefficients below.
    const val HOLE_RATIO = 0.266736f

    val GLSL_CORE = """
// Fourier fit of the Mewbo brand mark's outer silhouette (480x480 viewBox path, clay #C15F3C):
// r(theta)/R0 = 1 + a1*cos(8*theta) - a2*cos(16*theta) + a3*cos(24*theta). Coefficients were fit
// offline by sampling the real SVG path (945 points), converting to polar around its center, and
// taking an FFT (dominant 8th harmonic = the 8 lobes; 16th/24th sharpen the lobe tips/valleys).
// The overlay of this formula against the sampled path is visually indistinguishable.
float petalShape(float theta) {
    return 1.0
        + 0.076639 * cos(8.0 * theta)
        - 0.013403 * cos(16.0 * theta)
        + 0.005881 * cos(24.0 * theta);
}

// Silhouette coverage (1 = inside the flower + outside its center hole, 0 = outside) for a point
// `uv` already centered on the mark. `rotation` spins the lobes, `amp` scales how far the lobes
// deviate from a plain circle (0 = perfect circle, 1 = full petalShape), `aa` is the antialiasing
// window in the same units as uv (pass 1.5/m for a ~1.5px-wide edge regardless of draw size).
float flowerShapeAlpha(float2 uv, float rotation, float amp, float aa) {
    float dist = length(uv);
    float theta = atan(uv.y, uv.x) - rotation;
    float outerR = ${FLOWER_SCALE} * (1.0 + amp * (petalShape(theta) - 1.0));
    float holeR = ${FLOWER_SCALE} * ${HOLE_RATIO};
    float petalMask = 1.0 - smoothstep(outerR - aa, outerR + aa, dist);
    float holeMask = smoothstep(holeR - aa, holeR + aa, dist);
    return petalMask * holeMask;
}
"""
}
