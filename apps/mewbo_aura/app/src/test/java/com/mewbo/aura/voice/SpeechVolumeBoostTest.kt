package com.mewbo.aura.voice

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [SpeechVolumeBoost]'s rules, all of them pure or observable through a recording platform double.
 *
 * Plain JVM, no Robolectric, and that is the whole reason [AudioBoostPlatform] and
 * [SpeechVolumeBoostGate] are seams: the claims here are about clamping, unit conversion, whether
 * an effect is attached at all, and a refusal latch — none of which needs an `AudioManager`, a
 * `LoudnessEnhancer` or a `Context` to be true.
 *
 * The `CoroutineScope` is an independent one on the enclosing `TestScope`'s scheduler, per this
 * module's house idiom: the class holds a `stateIn` collector that by design never completes, which
 * `runTest`'s own leak check would otherwise flag.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SpeechVolumeBoostTest {

    // ---- Pure rules ----

    @Test
    fun `a level above the ceiling is clamped rather than passed through`() {
        assertEquals(SpeechVolumeBoost.MAX_DECIBELS, SpeechVolumeBoost.clampDecibels(99))
        assertEquals(SpeechVolumeBoost.MAX_DECIBELS, SpeechVolumeBoost.clampDecibels(SpeechVolumeBoost.MAX_DECIBELS))
        assertEquals(19, SpeechVolumeBoost.clampDecibels(19))
    }

    @Test
    fun `a negative level clamps to off, never to attenuation`() {
        // A boost control that could quieten the assistant is not the control anyone asked for,
        // and DataStore hands back whatever was written.
        assertEquals(SpeechVolumeBoost.OFF_DECIBELS, SpeechVolumeBoost.clampDecibels(-6))
        assertEquals(0, SpeechVolumeBoost.millibelsFor(-6))
    }

    @Test
    fun `decibels convert to millibels, the unit setTargetGain takes`() {
        assertEquals(0, SpeechVolumeBoost.millibelsFor(0))
        assertEquals(300, SpeechVolumeBoost.millibelsFor(3))
        assertEquals(1_000, SpeechVolumeBoost.millibelsFor(10))
    }

    @Test
    fun `the conversion cannot be routed around the clamp`() {
        // Same clamp on both, so an out-of-range level cannot reach the effect through the other
        // door — 99 dB would be 9900 mB if the conversion had its own arithmetic.
        assertEquals(SpeechVolumeBoost.MAX_DECIBELS * 100, SpeechVolumeBoost.millibelsFor(99))
    }

    @Test
    fun `every offered level survives the clamp, and off leads`() {
        // A level the picker offers but the clamp rewrites would silently pick a different gain
        // than the checkmark the user is looking at.
        SpeechVolumeBoost.LEVELS_DECIBELS.forEach { level ->
            assertEquals("level $level must survive its own clamp", level, SpeechVolumeBoost.clampDecibels(level))
        }
        assertEquals(SpeechVolumeBoost.OFF_DECIBELS, SpeechVolumeBoost.LEVELS_DECIBELS.first())
    }

    // ---- Attachment behaviour ----

    @Test
    fun `off attaches nothing at all`() = runTest {
        val platform = RecordingPlatform()
        val boost = boost(platform, decibels = SpeechVolumeBoost.OFF_DECIBELS)
        advanceUntilIdle()

        assertNull("off must yield no session to route audio through", boost.sessionId())
        assertEquals("off must not attach an effect at zero gain — it must not attach one", 0, platform.attachCalls)
        assertEquals(SpeechBoostState.Untested, boost.state.value)
    }

    @Test
    fun `an on level attaches once at the converted gain`() = runTest {
        val platform = RecordingPlatform()
        val boost = boost(platform, decibels = 6)
        advanceUntilIdle()

        val session = boost.sessionId()

        assertEquals(platform.lastIssuedSessionId, session)
        assertEquals(1, platform.attachCalls)
        assertEquals("6 dB must reach the effect as 600 mB", 600, platform.lastGainMillibels)
        assertEquals(SpeechBoostState.Applied(6), boost.state.value)
    }

    @Test
    fun `a multi-sentence reply reuses one effect rather than one per utterance`() = runTest {
        val platform = RecordingPlatform()
        val boost = boost(platform, decibels = 10)
        advanceUntilIdle()

        val first = boost.sessionId()
        val second = boost.sessionId()
        val third = boost.sessionId()

        assertEquals(first, second)
        assertEquals(second, third)
        assertEquals("three sentences must build ONE effect", 1, platform.attachCalls)
    }

    @Test
    fun `release drops the effect and the next run builds a fresh one`() = runTest {
        val platform = RecordingPlatform()
        val boost = boost(platform, decibels = 10)
        advanceUntilIdle()

        val first = boost.sessionId()
        boost.release()
        val second = boost.sessionId()

        assertEquals("the barge-in boundary must release the effect", 1, platform.releaseCalls)
        assertEquals(2, platform.attachCalls)
        assertNotEquals("a fresh run must not reuse the released run's session", first, second)
    }

    @Test
    fun `turning the boost off mid-life releases the live effect`() = runTest {
        val platform = RecordingPlatform()
        val level = MutableStateFlow(10)
        val boost = boost(platform, level)
        advanceUntilIdle()
        boost.sessionId()

        level.value = SpeechVolumeBoost.OFF_DECIBELS
        advanceUntilIdle()

        assertNull(boost.sessionId())
        assertEquals("off must not leave an effect attached behind it", 1, platform.releaseCalls)
    }

    @Test
    fun `a refused attach yields no session, so the caller keeps its untouched path`() = runTest {
        val platform = RecordingPlatform(refuse = true)
        val boost = boost(platform, decibels = 6)
        advanceUntilIdle()

        assertNull("a half-attached state must never be offered to a caller", boost.sessionId())
        assertEquals(SpeechBoostState.Refused(6), boost.state.value)
    }

    @Test
    fun `a refusal is retried once per level, not once per sentence`() = runTest {
        val platform = RecordingPlatform(refuse = true)
        val level = MutableStateFlow(6)
        val boost = boost(platform, level)
        advanceUntilIdle()

        repeat(5) { boost.sessionId() }
        assertEquals("a device-level refusal must not be re-attempted per utterance", 1, platform.attachCalls)

        level.value = 15
        advanceUntilIdle()
        boost.sessionId()
        assertEquals("a changed level must earn a fresh attempt", 2, platform.attachCalls)
    }

    @Test
    fun `a framework with no session to give is a refusal, not an attach on an invalid id`() = runTest {
        // `generateAudioSessionId` answers AudioManager.ERROR (-1) rather than throwing.
        val platform = RecordingPlatform(sessionIds = generateSequence { -1 }.iterator())
        val boost = boost(platform, decibels = 6)
        advanceUntilIdle()

        assertNull(boost.sessionId())
        assertEquals("an invalid session id must never reach the effect", 0, platform.attachCalls)
        assertEquals(SpeechBoostState.Refused(6), boost.state.value)
    }

    @Test
    fun `a stored level beyond the ceiling reaches the effect clamped`() = runTest {
        val platform = RecordingPlatform()
        val boost = boost(platform, decibels = 400)
        advanceUntilIdle()
        boost.sessionId()

        assertEquals(SpeechVolumeBoost.MAX_DECIBELS * 100, platform.lastGainMillibels)
        assertEquals(SpeechBoostState.Applied(SpeechVolumeBoost.MAX_DECIBELS), boost.state.value)
    }

    // ---- The state's own rule ----

    @Test
    fun `a refusal of one level is not a refusal of another`() {
        val refused = SpeechBoostState.Refused(3)

        assertTrue(refused.refuses(3))
        assertFalse("a level the user has since changed must not read as refused", refused.refuses(15))
        assertFalse(SpeechBoostState.Untested.refuses(3))
        assertFalse("an attach that SUCCEEDED must never render as a refusal", SpeechBoostState.Applied(3).refuses(3))
    }

    // ---- Harness ----

    private fun TestScope.boost(platform: RecordingPlatform, decibels: Int): SpeechVolumeBoost =
        boost(platform, MutableStateFlow(decibels))

    private fun TestScope.boost(platform: RecordingPlatform, level: MutableStateFlow<Int>): SpeechVolumeBoost =
        SpeechVolumeBoost(
            platform = platform,
            gate = { level },
            scope = CoroutineScope(StandardTestDispatcher(testScheduler) + SupervisorJob()),
        )

    /**
     * A platform that hands out distinct session ids and records what was asked of it.
     *
     * Ids are distinct on purpose: "the second run got a fresh session" is a claim a constant id
     * could not distinguish from "the first attachment was never released".
     */
    private class RecordingPlatform(
        private val refuse: Boolean = false,
        private val sessionIds: Iterator<Int> = generateSequence(1) { it + 1 }.iterator(),
    ) : AudioBoostPlatform {
        var attachCalls = 0
        var releaseCalls = 0
        var lastGainMillibels: Int? = null
        var lastIssuedSessionId: Int? = null

        override fun newSessionId(): Int = sessionIds.next().also { lastIssuedSessionId = it }

        override fun attachLoudness(sessionId: Int, gainMillibels: Int): BoostHandle? {
            attachCalls++
            lastGainMillibels = gainMillibels
            return if (refuse) null else BoostHandle { releaseCalls++ }
        }
    }
}
