package com.mewbo.aura.ui.orb

import android.os.Build

/**
 * The ONE capability gate for this app's AGSL layer.
 *
 * `android.graphics.RuntimeShader` is API 33+, and the app's minSdk is 30 — so on Android 11/12
 * (and the Fire TV / Android TV hardware that pins there) every AGSL surface would crash on
 * construction. Every shader-backed composable asks HERE, once, and early-returns to a plain
 * Compose fallback when the answer is false; the shader path itself carries
 * `@RequiresApi(Build.VERSION_CODES.TIRAMISU)` so lint proves the gate rather than being told to
 * ignore it. Deliberately not four scattered `SDK_INT` checks: a fifth shader surface that forgets
 * one is a crash on a device nobody in this loop is holding.
 *
 * It lives beside the other shared shader primitives ([GlslNoise], [ClayFlowerSdf],
 * `ShaderFrameClock`) because it is the same substrate — the aurora family imports it from here
 * exactly as it already imports [GlslNoise].
 *
 * The fallbacks are deliberately modest: the API 30-32 target is a television where these surfaces
 * are decorative, so each renders the same shape and palette as a static gradient with no attempt
 * to reproduce the shader's motion. There is no second animation system to keep in sync.
 */
internal object AuraShaders {
    /** True when `RuntimeShader` (API 33+) can be constructed on this device. */
    val supported: Boolean
        get() = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
}
