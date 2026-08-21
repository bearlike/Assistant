package com.mewbo.aura.notify

import com.mewbo.aura.R
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Contract for the pure decisions [RunNotifier] makes about the single ongoing notification: which
 * session its tap opens, which glyph states the mode, and which buttons it carries. Building and
 * posting the `Notification` itself is Android boundary code, deliberately not tested here
 * (`data/CLAUDE.md`: stub only I/O) — and a test asserting "a builder was called" would have no
 * power to fail on the thing that actually broke, which was a notification carrying no content
 * intent at all.
 *
 * The rule under test: the ongoing entry describes ONE run (the newest to start, whose label it
 * shows) and must open THAT run, with no target at all when there is no session to open.
 */
class RunNotifierTest {

    @Test
    fun `the tap opens the session the notification is reporting on`() {
        assertEquals("sess-42", RunNotifier.ongoingTapTarget("sess-42"))
    }

    @Test
    fun `a blank session id yields no tap target at all`() {
        // The service's state before any run is watched, and the Stop intent's. `AuraNavHost` gates
        // its handoff effect on `null`, NOT on blank — a blank id would navigate to `chat?sessionId=`
        // rather than doing nothing, so the content intent must be absent, not empty.
        assertNull(RunNotifier.ongoingTapTarget(""))
        assertNull(RunNotifier.ongoingTapTarget("   "))
        assertNull(RunNotifier.ongoingTapTarget(null))
    }

    @Test
    fun `the target follows the newest run, never latching onto the first`() {
        // One notification, N watched sessions: a second run re-posts in place and moves the label,
        // so the target must move with it. A stateful "first session wins" would leave the entry
        // describing one run and opening another — the exact disagreement this rule exists to
        // prevent.
        assertEquals("first", RunNotifier.ongoingTapTarget("first"))
        assertEquals("second", RunNotifier.ongoingTapTarget("second"))
    }

    @Test
    fun `device control gets its own status-bar glyph, not a recoloured one`() {
        // The status bar is the one piece of chrome visible without expanding anything, and
        // SystemUI tints every glyph in it with its OWN foreground colour — `setColor` reaches the
        // shade badge and never the status bar. So the mode has to be carried by the SHAPE. Assert
        // the two differ FIRST: unit-test `R` fields would compare equal if they were both 0, and a
        // test that cannot fail is worse than no test.
        assertNotEquals(
            RunNotifier.ongoingSmallIcon(deviceControl = false),
            RunNotifier.ongoingSmallIcon(deviceControl = true),
        )
        assertEquals(R.drawable.ic_launcher_monochrome, RunNotifier.ongoingSmallIcon(false))
        assertEquals(R.drawable.ic_stat_device_control, RunNotifier.ongoingSmallIcon(true))
    }

    @Test
    fun `an ordinary run carries Open and no Stop`() {
        // Open because a tappable notification body advertises nothing; no Stop because no server
        // operation means "stop this run, keep the session" — see RunNotifier.OngoingAction.STOP.
        assertEquals(
            listOf(RunNotifier.OngoingAction.OPEN),
            RunNotifier.ongoingActions("sess-42", deviceControl = false),
        )
    }

    @Test
    fun `device control adds Stop, and Open still comes first`() {
        // Order is render order, and the leftmost button is the one a thumb reaches from a pocket.
        // The harmless action takes that slot; the one that ends a grant does not.
        assertEquals(
            listOf(RunNotifier.OngoingAction.OPEN, RunNotifier.OngoingAction.STOP),
            RunNotifier.ongoingActions("sess-42", deviceControl = true),
        )
    }

    @Test
    fun `Open is absent when there is nowhere to open, but Stop is not`() {
        // `ongoingActions` takes the RESOLVED tap target so this can never disagree with the
        // content intent — an Open button that opens nothing is worse than no button. Stop needs no
        // target at all: it releases the app-wide grant and its own intent names no session, which
        // is exactly the state the service is in when the Stop intent re-enters `onStartCommand`.
        assertEquals(emptyList<RunNotifier.OngoingAction>(), RunNotifier.ongoingActions(null, false))
        assertEquals(
            listOf(RunNotifier.OngoingAction.STOP),
            RunNotifier.ongoingActions(null, deviceControl = true),
        )
    }

    @Test
    fun `the tap target decides Open, so a blank session id yields no button either`() {
        // The one composition that matters: `buildOngoing` resolves the target once and hands the
        // SAME answer to the content intent and to this. Feeding the raw id straight in is how the
        // two drift apart.
        val target = RunNotifier.ongoingTapTarget("   ")
        assertNull(target)
        assertEquals(emptyList<RunNotifier.OngoingAction>(), RunNotifier.ongoingActions(target, false))
    }
}
