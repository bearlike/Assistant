package com.mewbo.aura.voice

import android.os.VibrationEffect
import android.os.VibrationEffect.Composition
import android.os.Vibrator

/**
 * The §6.13 haptic map — four named moments, nothing else. Replaces the old per-state-kind tick
 * ([AssistTurnMachine] used to buzz on every visited state). [AssistTurnMachine] takes this as an
 * interface (not a concrete `Vibrator`-backed class) purely for JVM testability: `VibrationEffect`/
 * `Composition` are real Android SDK stub classes that throw "not mocked" outside Robolectric, so
 * unit tests inject a plain [AuraHaptics] fake that just records calls instead.
 */
interface AuraHaptics {
    /** t=0 of the §7.0 invocation timeline - fired once, before any drawing. */
    fun invocation()

    /** A voice-initiated turn's Final transcript is accepted (about to `AssistTurnMachine`'s
     * `beginTurn`). */
    fun transcriptAccepted()

    /** A turn hands off successfully - either `beginTurn`'s dispatched-query path or
     * `AssistTurnMachine.continueLastSession`'s dispatch-free one (v4: there's no more in-overlay
     * "stream to completion" to fold on; handing off IS the completion). */
    fun settle()

    /** The machine enters [AssistUiState.Error]. */
    fun error()
}

/** No haptic feedback at all - the default for hosts/tests that don't care (e.g. every existing
 * [AssistTurnMachine] unit test that isn't specifically asserting the haptic map). */
object NoOpAuraHaptics : AuraHaptics {
    override fun invocation() = Unit
    override fun transcriptAccepted() = Unit
    override fun settle() = Unit
    override fun error() = Unit
}

/**
 * The real, `Vibrator`-backed implementation. Each moment prefers the exact composition primitive
 * the spec names, falling back to the closest predefined effect on hardware that lacks composition
 * support ([Vibrator.areAllPrimitivesSupported] - some devices, and all AOSP-only emulators like
 * redroid, report no vibrator HAL at all, hence [vibrator] being nullable and every call wrapped in
 * `runCatching`).
 */
class VibratorAuraHaptics(private val vibrator: Vibrator?) : AuraHaptics {
    override fun invocation() = playPrimitive(Composition.PRIMITIVE_CLICK, INVOCATION_SCALE, VibrationEffect.EFFECT_CLICK)
    override fun transcriptAccepted() = playPrimitive(Composition.PRIMITIVE_TICK, FULL_SCALE, VibrationEffect.EFFECT_TICK)
    override fun settle() = playPrimitive(Composition.PRIMITIVE_TICK, SETTLE_SCALE, VibrationEffect.EFFECT_TICK)
    override fun error() = playDoubleClick()

    private fun playPrimitive(primitiveId: Int, scale: Float, fallbackEffect: Int) {
        runCatching {
            val v = vibrator ?: return@runCatching
            if (v.areAllPrimitivesSupported(primitiveId)) {
                v.vibrate(VibrationEffect.startComposition().addPrimitive(primitiveId, scale).compose())
            } else {
                v.vibrate(VibrationEffect.createPredefined(fallbackEffect))
            }
        }
    }

    private fun playDoubleClick() {
        runCatching {
            val v = vibrator ?: return@runCatching
            if (v.areAllPrimitivesSupported(Composition.PRIMITIVE_CLICK)) {
                v.vibrate(
                    VibrationEffect.startComposition()
                        .addPrimitive(Composition.PRIMITIVE_CLICK, FULL_SCALE)
                        .addPrimitive(Composition.PRIMITIVE_CLICK, FULL_SCALE, ERROR_CLICK_GAP_MS)
                        .compose(),
                )
            } else {
                v.vibrate(VibrationEffect.createPredefined(VibrationEffect.EFFECT_DOUBLE_CLICK))
            }
        }
    }

    private companion object {
        const val INVOCATION_SCALE = 0.8f
        const val SETTLE_SCALE = 0.4f
        const val FULL_SCALE = 1f
        const val ERROR_CLICK_GAP_MS = 80
    }
}
