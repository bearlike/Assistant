package com.mewbo.aura.voice

import android.media.MediaPlayer
import androidx.test.core.app.ApplicationProvider
import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.model.SpeechDirection
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.onSubscription
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowMediaPlayer
import org.robolectric.shadows.ShadowMediaPlayer.MediaInfo
import org.robolectric.shadows.util.DataSource

/**
 * The read-ahead contract: sentence N+1 is SYNTHESIZED while sentence N is still PLAYING, and no
 * two clips are ever audible at once.
 *
 * **Why this suite is not plain-JVM**, against this module's own "plain JVM is the default" rule:
 * the claim is about the overlap between a network call and real [MediaPlayer] playback, and
 * playback is the half a pure test cannot see. `SpeechQueueOutcomeTest` covers the failure POLICY
 * precisely because it is expressible without Android; this covers the SCHEDULING, which is not.
 * `ShadowMediaPlayer` gives a clip a real duration and a real completion callback, so "the next
 * synthesis started before this clip finished" is an observed ordering rather than an inferred one.
 *
 * The gap being closed: synthesis costs roughly a third of the playback it feeds, and awaiting it
 * only after the previous clip ended put all of that into the silence between two sentences.
 *
 * **`Dispatchers.IO`, not a `TestScope`.** The pump awaits a [MediaPlayer] completion driven by the
 * Robolectric main looper, which virtual time does not advance — so the coordination here is real
 * latches with timeouts, and every wait is bounded rather than a potential suite hang.
 */
@RunWith(RobolectricTestRunner::class)
class RemoteSynthesizerPipelineTest {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** Every shadow player created by the synthesizer, in creation order — one per clip that
     * actually reached playback. Completion is driven from the test rather than by a timer, so
     * "while the current one is still playing" is a state the test HOLDS, not a race it hopes to
     * win. */
    private val players = java.util.Collections.synchronizedList(mutableListOf<ShadowMediaPlayer>())

    @Before
    fun setUp() {
        // Long enough that the shadow's own timed completion can never fire during a test: every
        // clip ends when this suite says it does. `writeClip` mints a fresh temp file per
        // utterance, so the data source cannot be pre-registered by path — hence the provider.
        ShadowMediaPlayer.setMediaInfoProvider { MediaInfo(NEVER_ENDS_MS, 0) }
        ShadowMediaPlayer.setCreateListener { _, shadow ->
            shadow.setDataSource(DataSource.toDataSource("stub"))
            players += shadow
        }
    }

    @After
    fun tearDown() {
        scope.cancel()
        // Not reset to null: `setMediaInfoProvider` wraps its argument in `Optional.of`, so a null
        // clear throws. `@Before` reinstalls both on every test, which is what isolation needs.
        players.clear()
    }

    @Test
    fun `the next sentence is synthesized while the current one is still playing`() {
        val gateway = ScriptedGateway()
        val synthesizer = synthesizer(gateway)
        val events = EventRecorder(synthesizer)

        synthesizer.speak("m1:0", "First sentence.")
        synthesizer.speak("m1:1", "Second sentence.")

        // The first clip has started and is HELD mid-playback — nothing completes it until this
        // test says so, so the window below is a state rather than a race.
        events.awaitStarted("m1:0")
        assertTrue("the first clip must still be playing", events.doneIds().isEmpty())

        // THE CLAIM. Under the old strictly-serial pump the second synthesis was only issued after
        // the first clip's completion — which has not happened here — so this wait times out.
        assertTrue(
            "the second synthesis must begin during the first clip's playback",
            gateway.awaitSynthesisCount(2),
        )
        assertEquals("...and it must not have started playing yet", listOf("m1:0"), events.startedIds())

        finishClip(0)
        events.awaitStarted("m1:1")
        finishClip(1)
        events.awaitDone("m1:1")
        assertEquals(listOf("m1:0", "m1:1"), events.doneIds())
    }

    @Test
    fun `read-ahead never runs more than one synthesis at a time`() {
        // The gateway's TTS backend serves only two requests in parallel across ALL callers, so a
        // depth that grew with the reply length would starve every other client. Depth is one.
        val gateway = ScriptedGateway()
        val synthesizer = synthesizer(gateway)
        val events = EventRecorder(synthesizer)

        repeat(4) { index -> synthesizer.speak("m1:$index", "Sentence $index.") }

        repeat(4) { index ->
            events.awaitStarted("m1:$index")
            finishClip(index)
        }

        events.awaitDone("m1:3")
        assertEquals(listOf("m1:0", "m1:1", "m1:2", "m1:3"), events.doneIds())
        assertEquals("at most one synthesis may ever be in flight", 1, gateway.peakConcurrency())
    }

    @Test
    fun `only one clip is ever playing at a time`() {
        // Overlapping audio is worse than a gap: two voices at once is unintelligible, where a gap
        // is merely slow. Asserted against the real player's own start/completion callbacks.
        val gateway = ScriptedGateway()
        val synthesizer = synthesizer(gateway)
        val events = EventRecorder(synthesizer)

        repeat(3) { index -> synthesizer.speak("m1:$index", "Sentence $index.") }

        repeat(3) { index ->
            events.awaitStarted("m1:$index")
            // The next clip must not have begun while this one is unfinished — checked here, on
            // every sentence, rather than only inferred from the timeline at the end.
            assertEquals("no clip may start before the previous one completes", index + 1, events.startedIds().size)
            finishClip(index)
        }

        events.awaitDone("m1:2")
        assertEquals("no two utterances may be audible at once", 1, events.peakPlaying())
        // Strictly alternating Started/Done proves the serialization directly: an overlap would
        // show as two Starteds with no Done between them.
        assertEquals(
            listOf("+m1:0", "-m1:0", "+m1:1", "-m1:1", "+m1:2", "-m1:2"),
            events.timeline(),
        )
    }

    @Test
    fun `a sentence arriving mid-playback joins the running pump - it never starts a second one`() {
        // THE STREAMING CASE, and the one the other tests here cannot see because they enqueue
        // everything up front. A live reply produces its next sentence WHILE the previous one is
        // playing, so the queue is legitimately empty at the moment the read-ahead looks. Treating
        // that emptiness as "this run is over" retires the pump mid-clip, and the next `speak()`
        // then launches a SECOND pump alongside the audio still playing — two voices at once, which
        // is worse than any gap.
        val gateway = ScriptedGateway()
        val synthesizer = synthesizer(gateway)
        val events = EventRecorder(synthesizer)

        synthesizer.speak("m1:0", "First sentence.")
        events.awaitStarted("m1:0")
        gateway.awaitSynthesisCount(1)

        // The stream delivers the next sentence while the first is still playing. The correct pump
        // already made its read-ahead poll (before playback, and the queue was empty then), so it
        // will not look again until clip 0 completes — which only this test can trigger. A SECOND
        // pump, by contrast, would start from `speak()` immediately: synthesize, then play.
        //
        // So the discriminator is simply "did anything at all happen while clip 0 is held?".
        // Correct: no second synthesis, no second player. Defective: both, promptly.
        synthesizer.speak("m1:1", "Arrived mid-playback.")
        drainLooper()

        assertEquals("nothing may be synthesized while the pump is mid-clip", 1, gateway.synthesisCount())
        assertEquals("it must not start playing over the current clip", listOf("m1:0"), events.startedIds())
        assertEquals("a second pump would construct a second player", 1, players.size)

        finishClip(0)
        events.awaitStarted("m1:1")
        finishClip(1)
        events.awaitDone("m1:1")

        assertEquals(listOf("m1:0", "m1:1"), events.doneIds())
        assertEquals("no two utterances may be audible at once", 1, events.peakPlaying())
        assertEquals(listOf("+m1:0", "-m1:0", "+m1:1", "-m1:1"), events.timeline())
    }

    @Test
    fun `a barge-in mid-read-ahead drops the prefetched clip - it never plays`() {
        // A prefetched clip that plays after a barge-in is the serious defect this whole read-ahead
        // could have introduced: the user asked for silence and got a sentence anyway.
        val gateway = ScriptedGateway()
        val synthesizer = synthesizer(gateway)
        val events = EventRecorder(synthesizer)

        synthesizer.speak("m1:0", "First sentence.")
        synthesizer.speak("m1:1", "Second sentence.")
        gateway.awaitSynthesisCount(2)
        events.awaitStarted("m1:0")

        // The read-ahead has ALREADY fetched the second sentence and is holding its clip — the
        // exact state in which a barge-in could leak audio.
        synthesizer.stop()

        // Gated already: the prefetch was awaited above, so the clip that must never play is in
        // hand at this point. This only gives it the chance to escape.
        drainLooper()

        assertEquals("the prefetched sentence must never be spoken", listOf("m1:0"), events.startedIds())
        assertEquals("and no second clip may ever be constructed", 1, players.size)
        // The flush stopped the audio that WAS playing, rather than only dropping the queue. This
        // is also why the held clip can never resume: its player is released, not merely paused.
        assertFalse("barge-in must stop the clip in flight", players[0].isReallyPlaying)
    }

    @Test
    fun `a failed sentence ends the read and ANSWERS the prefetched one it drops`() {
        // Two laws at once. A failure ends the read rather than skipping ahead (a sentence
        // vanishing from the middle of a reply is worse than the audio stopping) — and every
        // dropped utterance still gets an Error, because `SpeechController` clears its speaking
        // state only on an event for its own last-enqueued id. The read-ahead makes the second law
        // easy to break: the prefetched utterance has already left the queue, so the queue drain
        // cannot see it.
        val gateway = ScriptedGateway(failFrom = 1)
        val synthesizer = synthesizer(gateway)
        val events = EventRecorder(synthesizer)

        synthesizer.speak("m1:0", "First sentence.")
        synthesizer.speak("m1:1", "This one fails.")
        synthesizer.speak("m1:2", "Never reached.")

        events.awaitStarted("m1:0")
        finishClip(0)
        events.awaitError("m1:2")
        assertEquals("the failure must not be skipped past", listOf("m1:0"), events.doneIds())
        assertEquals("every dropped utterance is answered", listOf("m1:1", "m1:2"), events.errorIds())
    }

    // ---- fixtures ----

    /**
     * End the [index]-th clip's playback, as the real player's completion callback would.
     *
     * Waits for that player to be genuinely PLAYING first, not merely constructed. The shadow's
     * create listener fires inside the [MediaPlayer] constructor — before `setDataSource`,
     * `prepare`, the completion listener or `start()` — so completing on mere existence would fire
     * a callback the pump has not yet attached, and the run would hang holding a clip that already
     * ended. That is a harness race, and it produced a real red here.
     */
    /**
     * Give any already-scheduled work its chance to run, then settle the main looper.
     *
     * Used only where the assertion is that something did NOT happen. Observing an absence needs a
     * bounded wait by nature — but every such site here first GATES on a positive event (the
     * prefetch, the barge-in) so the wrong behaviour is already imminent rather than merely
     * possible; this only lets it surface.
     */
    private fun drainLooper() {
        repeat(SETTLE_POLLS) {
            shadowOf(android.os.Looper.getMainLooper()).idle()
            Thread.sleep(POLL_MILLIS)
        }
        shadowOf(android.os.Looper.getMainLooper()).idle()
    }

    private fun finishClip(index: Int) {
        val deadline = System.currentTimeMillis() + AWAIT_SECONDS * 1_000
        while (players.size <= index || !players[index].isReallyPlaying) {
            if (System.currentTimeMillis() > deadline) throw AssertionError("clip $index never started playing")
            shadowOf(android.os.Looper.getMainLooper()).idle()
            Thread.sleep(POLL_MILLIS)
        }
        players[index].invokeCompletionListener()
    }

    private fun synthesizer(gateway: SpeechGateway) = RemoteSynthesizer(
        context = ApplicationProvider.getApplicationContext(),
        gateway = gateway,
        engineGate = { MutableStateFlow("supertonic-3") },
        // Boost OFF, which is the untouched default and — the point for this suite — the state in
        // which `SpeechVolumeBoost` hands out no session id and touches no platform effect at all.
        // The pipeline claims below (read-ahead depth, ordering, focus) are therefore measured
        // against exactly the playback path that shipped before the boost existed.
        boost = SpeechVolumeBoost(
            platform = RefusingBoostPlatform,
            gate = { MutableStateFlow(SpeechVolumeBoost.OFF_DECIBELS) },
            scope = scope,
        ),
        scope = scope,
    )

    /** Fails any attach it is asked for, and is never asked: the gate above is OFF. Present so a
     * regression that started attaching unconditionally would be visible here as a `MediaPlayer`
     * session change rather than as nothing. */
    private object RefusingBoostPlatform : AudioBoostPlatform {
        override fun newSessionId(): Int = -1

        override fun attachLoudness(sessionId: Int, gainMillibels: Int): BoostHandle? = null
    }

    /**
     * Records the in-flight concurrency of [synthesize] as well as its call count — the depth claim
     * is about simultaneity, which a call count alone cannot distinguish from a fast sequence.
     */
    private class ScriptedGateway(private val failFrom: Int = -1) : SpeechGateway {
        private val started = AtomicInteger(0)
        private val inFlight = AtomicInteger(0)
        private val peak = AtomicInteger(0)
        private val calls = mutableListOf<CountDownLatch>()

        @Synchronized
        private fun latchFor(count: Int): CountDownLatch {
            while (calls.size < count) calls += CountDownLatch(1)
            return calls[count - 1]
        }

        override suspend fun synthesize(modelId: String, text: String): ByteArray {
            val ordinal = started.incrementAndGet()
            val depth = inFlight.incrementAndGet()
            peak.updateAndGet { maxOf(it, depth) }
            try {
                latchFor(ordinal).countDown()
                if (failFrom >= 0 && ordinal - 1 >= failFrom) throw java.io.IOException("gateway 502")
                return ByteArray(64) { it.toByte() }
            } finally {
                inFlight.decrementAndGet()
            }
        }

        override suspend fun transcribe(modelId: String, audio: ByteArray): String =
            throw UnsupportedOperationException("not exercised by this suite")

        /** `false` on timeout rather than an exception, so the CALLER states what the miss means. */
        fun awaitSynthesisCount(count: Int): Boolean =
            latchFor(count).await(AWAIT_SECONDS, TimeUnit.SECONDS)

        fun peakConcurrency(): Int = peak.get()

        /** Total [synthesize] calls begun. Used where the claim is that NOTHING was fetched, which
         * no latch can express — a latch only ever waits for a call that does happen. */
        fun synthesisCount(): Int = started.get()
    }

    /**
     * Collects [SynthEvent]s off the synthesizer's own flow, deriving the two orderings the suite
     * asserts: how many clips were audible at once, and the exact Started/Done interleaving.
     */
    private inner class EventRecorder(synthesizer: Synthesizer) {
        private val seen = mutableListOf<SynthEvent>()
        private val marks = mutableListOf<String>()
        private var playing = 0
        private var peakPlaying = 0

        /**
         * **Blocks until this collector is actually subscribed**, and that is what makes the suite
         * deterministic rather than merely usually-green.
         *
         * `RemoteSynthesizer._events` is a `MutableSharedFlow` with NO replay, emitted through
         * `tryEmit` — so an event published while nobody is subscribed is DROPPED, permanently. The
         * collector here starts on a real dispatcher, so without this gate the pump could emit
         * `Started` before the collector attached and the event simply never existed: `awaitStarted`
         * then timed out no matter how long it waited. Measured at 5 failures in 10 runs.
         *
         * A lost update, not a narrow window — which is why the cure is a happens-before edge and
         * not a longer timeout.
         */
        init {
            // `onSubscription` (not `onStart`) is the operator that fires only once the subscriber
            // is REGISTERED — `onStart` runs before registration and would reinstate the race. It
            // is declared on SharedFlow, so this names the requirement instead of assuming it.
            val stream = synthesizer.events() as? SharedFlow<SynthEvent>
                ?: error("RemoteSynthesizer publishes a SharedFlow; the subscription gate needs one")
            val subscribed = CountDownLatch(1)
            stream
                .onSubscription { subscribed.countDown() }
                .onEach { event -> record(event) }
                .launchIn(scope)
            check(subscribed.await(AWAIT_SECONDS, TimeUnit.SECONDS)) { "the event collector never subscribed" }
        }

        @Synchronized
        private fun record(event: SynthEvent) {
            seen += event
            when (event) {
                is SynthEvent.Started -> {
                    playing++
                    peakPlaying = maxOf(peakPlaying, playing)
                    marks += "+${event.id}"
                }
                is SynthEvent.Done -> {
                    playing--
                    marks += "-${event.id}"
                }
                is SynthEvent.Error -> playing = 0
            }
        }

        @Synchronized private fun snapshot(): List<SynthEvent> = seen.toList()

        @Synchronized fun peakPlaying(): Int = peakPlaying

        @Synchronized fun timeline(): List<String> = marks.toList()

        fun startedIds(): List<String> = snapshot().filterIsInstance<SynthEvent.Started>().map { it.id }
        fun doneIds(): List<String> = snapshot().filterIsInstance<SynthEvent.Done>().map { it.id }
        fun errorIds(): List<String> = snapshot().filterIsInstance<SynthEvent.Error>().map { it.id }

        fun awaitStarted(id: String) = await("Started($id)") { SynthEvent.Started(id) in snapshot() }
        fun awaitDone(id: String) = await("Done($id)") { SynthEvent.Done(id) in snapshot() }
        fun awaitError(id: String) = await("Error($id)") { SynthEvent.Error(id) in snapshot() }

        /** Polls the main looper too: [MediaPlayer]'s completion callback is posted there, so a
         * bare sleep would wait forever for a clip that never finishes. */
        private fun await(what: String, condition: () -> Boolean) {
            val deadline = System.currentTimeMillis() + AWAIT_SECONDS * 1_000
            while (System.currentTimeMillis() < deadline) {
                shadowOf(android.os.Looper.getMainLooper()).idle()
                if (condition()) return
                Thread.sleep(POLL_MILLIS)
            }
            throw AssertionError("timed out waiting for $what; saw ${timeline()}")
        }
    }

    private companion object {
        const val AWAIT_SECONDS = 10L
        const val POLL_MILLIS = 5L

        /** A clip long enough that the shadow's own timed completion never fires mid-test; every
         * clip in this suite ends via `finishClip`. */
        const val NEVER_ENDS_MS = 600_000

        /** Poll iterations `drainLooper` spends letting a wrong behaviour surface, after the test
         * has already gated on the positive event that would precede it. */
        const val SETTLE_POLLS = 40
    }
}
