package com.mewbo.aura.ui.aurora

import androidx.compose.animation.core.TweenSpec
import com.mewbo.aura.ui.theme.AuraMotion
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AuroraWashUniformMathTest {

    @Test
    fun `hidden targets zero intensity, active states target full`() {
        assertEquals(0f, AuroraWashUniformMath.targetIntensity(AuroraState.Hidden))
        assertEquals(1f, AuroraWashUniformMath.targetIntensity(AuroraState.Resting))
        assertEquals(1f, AuroraWashUniformMath.targetIntensity(AuroraState.Thinking))
        assertEquals(1f, AuroraWashUniformMath.targetIntensity(AuroraState.Streaming))
    }

    @Test
    fun `streaming drift matches the named edge hue-rotation rate, thinking is subtler, resting is calmest`() {
        assertEquals(AuraMotion.edgeHueRotationHz, AuroraWashUniformMath.driftHz(AuroraState.Streaming))
        assertTrue(
            AuroraWashUniformMath.driftHz(AuroraState.Thinking) <
                AuroraWashUniformMath.driftHz(AuroraState.Streaming),
        )
        assertTrue(
            AuroraWashUniformMath.driftHz(AuroraState.Resting) <
                AuroraWashUniformMath.driftHz(AuroraState.Thinking),
        )
        assertEquals(0f, AuroraWashUniformMath.driftHz(AuroraState.Hidden))
    }

    @Test
    fun `entering an active state ramps in over M3, settling to hidden fades out over M4`() {
        val rampIn = AuroraWashUniformMath.transitionSpec(targetIntensity = 1f) as TweenSpec<Float>
        val fadeOut = AuroraWashUniformMath.transitionSpec(targetIntensity = 0f) as TweenSpec<Float>

        assertEquals(AuraMotion.thinkingRampMs, rampIn.durationMillis)
        assertEquals(AuraMotion.settleFadeMs, fadeOut.durationMillis)
    }
}
