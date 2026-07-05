package com.mewbo.aura.ui.orb

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SparkUniformMathTest {

    @Test
    fun `thinking spins and sweeps faster than shimmer`() {
        assertTrue(
            SparkUniformMath.rotationRevsPerSec(SparkState.Thinking) >
                SparkUniformMath.rotationRevsPerSec(SparkState.Shimmer),
        )
        assertTrue(SparkUniformMath.sweepHz(SparkState.Thinking) > SparkUniformMath.sweepHz(SparkState.Shimmer))
    }

    @Test
    fun `thinking dials back shimmer strength relative to the resting shimmer loop`() {
        assertTrue(
            SparkUniformMath.shimmerStrength(SparkState.Thinking) <
                SparkUniformMath.shimmerStrength(SparkState.Shimmer),
        )
    }

    @Test
    fun `shimmer holds a constant lobe amplitude, thinking pulses around it`() {
        assertEquals(1f, SparkUniformMath.lobeAmplitude(SparkState.Shimmer, timeSeconds = 0f), 1e-6f)
        assertEquals(1f, SparkUniformMath.lobeAmplitude(SparkState.Shimmer, timeSeconds = 123.4f), 1e-6f)

        val samples = (0..200).map { i -> SparkUniformMath.lobeAmplitude(SparkState.Thinking, timeSeconds = i * 0.01f) }
        assertTrue(samples.maxOrNull()!! > 1f)
        assertTrue(samples.minOrNull()!! < 1f)
    }
}
