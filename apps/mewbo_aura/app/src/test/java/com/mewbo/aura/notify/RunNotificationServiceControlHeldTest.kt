package com.mewbo.aura.notify

import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `RunNotificationService.recordControlHeld` — the claim that a grant's end is claimed EXACTLY
 * ONCE, whichever of the two observers gets there first.
 *
 * **What is actually at stake.** `returnToApp()` starts an Activity. Two callers watch the same
 * drop: the `DeviceControlSession.active` collector on `Dispatchers.Default`, and the `EXTRA_STOP`
 * branch of `onStartCommand` on the main thread. Both are needed — the collector is the only thing
 * that sees a grant LOST to a dead binder, and the Stop branch tears the service down without
 * waiting for the collector, so a drop claimed only by the collector would be lost on the path the
 * user actually takes. The cost of getting the dedup wrong is not a wasted call: it is the user
 * being yanked into the app twice.
 *
 * **Only the state logic is under test here, and it is the whole dedup.** The service is
 * constructed without a lifecycle — no `onCreate`, no Hilt injection, so every `@Inject lateinit`
 * collaborator stays unset and nothing here reaches the platform. `returnToApp`'s own three guards
 * (a resolved tap target, `AppForegroundChecker`, `runCatching`) are NOT covered by anything;
 * they need a real service and are still only reasoned.
 */
class RunNotificationServiceControlHeldTest {

    /**
     * A `StateFlow` replays its current value to a new collector, so the collector's FIRST emission
     * for a process that never took a grant is `false` — and a plain `!held` would read that as a
     * grant ending and pull the user into the app out of nowhere.
     */
    @Test
    fun `an initial false is not a drop — no grant preceded it`() {
        val service = RunNotificationService()

        assertFalse(service.recordControlHeld(false))
    }

    /** The transition itself: held, then not held, is the one call that reports the drop. */
    @Test
    fun `a true then false reports the drop exactly once`() {
        val service = RunNotificationService()

        assertFalse("taking the grant is not a drop", service.recordControlHeld(true))
        assertTrue("the transition is the drop", service.recordControlHeld(false))
    }

    /**
     * The loser of the race computes `false` and does nothing.
     *
     * Sequentially this is the shape both observers produce: the Stop branch claims the drop, the
     * collector's own emission arrives afterwards and must add nothing.
     */
    @Test
    fun `a repeated false after the drop reports nothing further`() {
        val service = RunNotificationService()
        service.recordControlHeld(true)

        assertTrue(service.recordControlHeld(false))
        assertFalse(service.recordControlHeld(false))
        assertFalse(service.recordControlHeld(false))
    }

    /** A grant taken again after a drop is a new lifetime, and its end is a new drop. */
    @Test
    fun `a second grant reports its own drop`() {
        val service = RunNotificationService()

        service.recordControlHeld(true)
        assertTrue(service.recordControlHeld(false))
        service.recordControlHeld(true)
        assertTrue("a later grant's end is its own transition", service.recordControlHeld(false))
    }

    /**
     * **The claim `@Synchronized` exists for, asserted rather than promised.**
     *
     * The two real observers run on different threads by design — the grant collector on
     * `Dispatchers.Default`, the Stop branch on the main thread — so "whichever observes it first
     * wins" is a concurrency property and no sequential test can see it. Every round below takes the
     * grant and then releases [RACERS] threads onto the same drop; total claims across [ROUNDS]
     * rounds must equal the round count exactly. Unsynchronised, the read and the write are two
     * steps, so two threads can both read `true` and both report the drop — two `startActivity`
     * calls, the user yanked into the app twice.
     *
     * **The start gate is a HOT SPIN and that is what gives the test its power.** An earlier version
     * used a thread pool and a `CyclicBarrier`; it passed with the lock REMOVED, because submitting
     * to an executor and unparking parked threads staggers them by microseconds while the window
     * being raced is a volatile read followed by a volatile write — tens of nanoseconds. Racers that
     * never overlap cannot collide. Spinning on an atomic round counter means every racer reacts
     * within its own spin loop, and the collision is then routine rather than hoped for: measured
     * over 20 000 rounds against an unsynchronised copy of this method, ~27% of rounds produced an
     * extra claim, so [ROUNDS] is far past the point of reliable detection.
     *
     * It can only ever fail in one direction. With the lock in place `wasHeld && !held` is true for
     * at most one caller per grant by construction, so a green here is never luck; a red is always a
     * real second claim.
     */
    @Test(timeout = 120_000L)
    fun `racing observers of one drop yield exactly one claim`() {
        val service = RunNotificationService()
        val claims = AtomicInteger(0)
        // The round the racers should be working on. Negative is the shutdown signal — the racers
        // are daemons besides, so a wedged round is the method timeout's problem, never the suite's.
        val round = AtomicInteger(0)
        val finished = AtomicInteger(0)

        val racers = List(RACERS) { index ->
            Thread({
                var seen = 0
                while (true) {
                    var current = round.get()
                    while (current == seen) {
                        Thread.onSpinWait()
                        current = round.get()
                    }
                    if (current < 0) return@Thread
                    seen = current
                    if (service.recordControlHeld(false)) claims.incrementAndGet()
                    finished.incrementAndGet()
                }
            }, "control-drop-racer-$index").apply { isDaemon = true }
        }
        racers.forEach { it.start() }

        try {
            repeat(ROUNDS) {
                service.recordControlHeld(true)
                // Reset BEFORE opening the gate: a racer cannot reach its increment for the next
                // round until it observes the new round number, which is published after this.
                finished.set(0)
                round.incrementAndGet()
                while (finished.get() < RACERS) Thread.onSpinWait()
            }
        } finally {
            round.set(-1)
        }

        assertEquals("one grant ending is one return to the app", ROUNDS, claims.get())
    }

    private companion object {
        /** More than the two real observers, so the window is hit rather than hoped for. */
        const val RACERS = 8

        /** Enough attempts that an unsynchronised read-then-write is OBSERVED rather than survived,
         * with a wide margin over the measured per-round collision rate — and small enough that the
         * whole race costs a few seconds of a suite this size. */
        const val ROUNDS = 300
    }
}
