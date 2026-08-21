package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.DeviceControlBinder
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.device.shizuku.DeviceControlStatusSource
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The grant's state machine, exercised with NO Shizuku binder anywhere — which
 * is the entire reason its two collaborators are narrow seams. Every refusal
 * below is reachable on a real phone and none of them is reachable in a test
 * that needs the real thing to be installed.
 */
class DeviceControlSessionTest {

    /** The house idiom for a class with an infinite `init`-block collector
     * (`app/src/test/java/com/mewbo/aura/CLAUDE.md`): an INDEPENDENT scope on the test's own
     * scheduler. `advanceUntilIdle()` still drives it, and `runTest`'s leak check ignores it —
     * `backgroundScope` fails the first half and the `TestScope` itself fails the second. */
    private fun TestScope.machineScope(): CoroutineScope =
        CoroutineScope(StandardTestDispatcher(testScheduler) + SupervisorJob())

    private fun TestScope.session(
        status: DeviceControlStatus = DeviceControlStatus.Ready,
        binds: Boolean = true,
    ) = DeviceControlSession(
        DeviceControlStatusSource { MutableStateFlow(status) },
        DeviceControlBinder { binds },
        machineScope(),
    )

    private fun TestScope.session(status: MutableStateFlow<DeviceControlStatus>) =
        DeviceControlSession(
            DeviceControlStatusSource { status },
            DeviceControlBinder { true },
            machineScope(),
        )

    @Test
    fun `a ready, bindable device grants`() = runTest {
        val session = session()

        assertSame(DeviceControlGrant.Granted, session.start())
        assertTrue(session.isActive())
        assertEquals(null, session.controlRefusal())
    }

    @Test
    fun `starting twice is idempotent and says so`() = runTest {
        val session = session()

        session.start()
        val second = session.start()

        assertSame(DeviceControlGrant.AlreadyActive, second)
        assertTrue("an already_active answer must leave the grant held", session.isActive())
    }

    @Test
    fun `each status maps to its own refusal, and the mapping is total`() = runTest {
        // Four causes, four remedies. Collapsing any pair into "unavailable"
        // sends the user to the wrong screen.
        assertSame(DeviceControlGrant.ShizukuNotInstalled, session(DeviceControlStatus.NotInstalled).start())
        assertSame(DeviceControlGrant.ShizukuNotRunning, session(DeviceControlStatus.NotRunning).start())
        assertSame(DeviceControlGrant.PermissionDenied, session(DeviceControlStatus.PermissionDenied).start())
    }

    @Test
    fun `a refusal never takes the grant`() = runTest {
        val session = session(DeviceControlStatus.PermissionDenied)

        session.start()

        assertFalse(session.isActive())
    }

    @Test
    fun `every substrate refusal names something the USER can act on`() {
        // The message is the only signal a person holding the phone gets, so an
        // empty or code-shaped one is the failure this union exists to remove.
        val refusals = listOf(
            DeviceControlGrant.ShizukuNotInstalled,
            DeviceControlGrant.ShizukuNotRunning,
            DeviceControlGrant.PermissionDenied,
        )

        refusals.forEach {
            assertFalse("${it.code} is active", it.active)
            assertTrue("${it.code} has no message", it.message.isNotBlank())
            assertTrue("${it.code} does not name the user", it.message.contains("user"))
        }
    }

    @Test
    fun `NotStarted is the one refusal the MODEL fixes, and start never returns it`() = runTest {
        assertTrue(DeviceControlGrant.NotStarted.message.contains("device_control_start"))

        // Reachable only from the answer seam. A start that returned it would be
        // telling the caller to call the thing it just called.
        val fromStart = listOf(
            DeviceControlStatus.NotInstalled,
            DeviceControlStatus.NotRunning,
            DeviceControlStatus.PermissionDenied,
            DeviceControlStatus.Ready,
        ).map { session(it).start() }

        assertTrue(fromStart.none { it === DeviceControlGrant.NotStarted })
    }

    // --- the substrate can go away under a live grant ---

    @Test
    fun `a binder death INVALIDATES a held grant rather than letting it lie`() = runTest {
        // The Shizuku user service is daemon(false): it dies with its client
        // process, so this is ordinary rather than exotic. Measured on-device —
        // the server went away mid-session and the only symptom was a tool count
        // quietly changing.
        val status = MutableStateFlow<DeviceControlStatus>(DeviceControlStatus.Ready)
        val session = session(status)
        session.start()
        assertTrue(session.isActive())

        status.value = DeviceControlStatus.NotRunning

        assertFalse("a grant may not outlive its binder", session.isActive())
    }

    @Test
    fun `a lost grant refuses with the SUBSTRATE reason, not with NotStarted`() = runTest {
        // Reporting NotStarted here would send the model to start, which would
        // refuse for the identical reason — a loop neither party can break.
        val status = MutableStateFlow<DeviceControlStatus>(DeviceControlStatus.Ready)
        val session = session(status)
        session.start()

        status.value = DeviceControlStatus.PermissionDenied

        assertSame(DeviceControlGrant.PermissionDenied, session.controlRefusal())
    }

    @Test
    fun `an ungranted session refuses with NotStarted`() = runTest {
        assertSame(DeviceControlGrant.NotStarted, session().controlRefusal())
    }

    @Test
    fun `a held grant refuses nothing`() = runTest {
        val session = session()
        session.start()

        assertEquals(null, session.controlRefusal())
    }

    @Test
    fun `a grant lost and then restored requires a fresh start, never resurrecting itself`() = runTest {
        // The stale intent must not re-arm the moment the substrate returns: the
        // agent that held it is long gone, and the user saw the notification go.
        val status = MutableStateFlow<DeviceControlStatus>(DeviceControlStatus.Ready)
        val session = session(status)
        session.start()

        status.value = DeviceControlStatus.NotRunning
        // The latch is a COLLECTOR, and a StateFlow conflates: without letting it
        // observe the loss, the two writes below collapse into "still Ready" and
        // nothing is invalidated. On a device the two transitions are a service
        // dying and a person restarting it, so they are never this close.
        advanceUntilIdle()
        status.value = DeviceControlStatus.Ready
        advanceUntilIdle()

        assertFalse("a returning binder must not resurrect a dead grant", session.isActive())
        // ...and the advice flips with the facts: while Shizuku was down the
        // refusal named Shizuku, but now that it is back the only thing left to
        // do is start again, so naming Shizuku would send the user to fix
        // something that is no longer broken.
        assertSame(DeviceControlGrant.NotStarted, session.controlRefusal())
        assertSame(DeviceControlGrant.Granted, session.start())
    }

    @Test
    fun `active drops to false on a binder death with nobody calling stop`() = runTest {
        // notify/ collects this to release the FGS hold and the persistent
        // notification, so the invalidation has to reach it as a VALUE.
        val status = MutableStateFlow<DeviceControlStatus>(DeviceControlStatus.Ready)
        val session = session(status)
        val seen = mutableListOf<Boolean>()
        val job = launch { session.active.toList(seen) }
        advanceUntilIdle()

        session.start()
        advanceUntilIdle()
        status.value = DeviceControlStatus.NotRunning
        advanceUntilIdle()

        assertEquals(listOf(false, true, false), seen)
        job.cancel()
    }

    @Test
    fun `stop on a lost grant still reports that it released something`() = runTest {
        // The binder is gone but the hold and the notification are not, and
        // releasing those is real work this call did.
        val status = MutableStateFlow<DeviceControlStatus>(DeviceControlStatus.Ready)
        val session = session(status)
        session.start()
        status.value = DeviceControlStatus.NotRunning

        assertTrue(session.stop())
        assertFalse(session.stop())
    }

    @Test
    fun `a status that permits a bind but whose bind FAILS is a refusal, not a hollow grant`() = runTest {
        // Ready says Shizuku would allow a bind; it does not say one succeeded.
        // Reporting granted here defers the failure to the first device_ui
        // call, which is where it used to be discovered.
        val session = session(status = DeviceControlStatus.Ready, binds = false)

        val outcome = session.start()

        assertSame(DeviceControlGrant.ShizukuNotRunning, outcome)
        assertFalse(session.isActive())
    }

    @Test
    fun `stop releases, and reports whether it was the one that did`() = runTest {
        val session = session()
        session.start()

        assertTrue("the first stop ended a live grant", session.stop())
        assertFalse(session.isActive())
        assertFalse("a second stop found nothing to end", session.stop())
    }

    @Test
    fun `stop on a grant that was never started is a normal call`() = runTest {
        // Every automatic release path races the others by design — the run's
        // terminal event, the notification's Stop, an explicit tool call.
        val session = session()

        assertFalse(session.stop())
        assertFalse(session.isActive())
    }

    @Test
    fun `a released grant can be taken again`() = runTest {
        val session = session()

        session.start()
        session.stop()

        assertSame(DeviceControlGrant.Granted, session.start())
    }

    @Test
    fun `canTakeControl tracks the status, not the grant`() = runTest {
        // The two questions the two gate layers ask. Conflating them is what
        // makes a tool advertised-but-unanswerable, or the reverse.
        assertTrue(session(DeviceControlStatus.Ready).canTakeControl())
        assertFalse(session(DeviceControlStatus.NotRunning).canTakeControl())

        val ready = session(DeviceControlStatus.Ready)
        assertTrue("control is possible before it is held", ready.canTakeControl())
        assertFalse(ready.isActive())
    }

    @Test
    fun `changes does NOT emit for a grant transition, which cannot move the advertised set`() = runTest {
        // A collector re-runs a real fetch — two HTTP calls — so an emission
        // that cannot change the answer is pure cost. Advertisement reads the
        // STATUS and the user's toggles; neither moves when a grant starts.
        val session = session()
        val seen = mutableListOf<Unit>()
        val job = launch { session.changes.toList(seen) }
        advanceUntilIdle()

        session.start()
        advanceUntilIdle()
        session.stop()
        advanceUntilIdle()

        assertEquals(0, seen.size)
        job.cancel()
    }

    @Test
    fun `changes emits when the Shizuku status moves under a still-idle grant`() = runTest {
        // The measured defect: authorising Shizuku happens in ANOTHER app, so
        // nothing in Aura is touched and every surface holding a tool count
        // keeps a stale one until the process restarts.
        val status = MutableStateFlow<DeviceControlStatus>(DeviceControlStatus.PermissionDenied)
        val session = session(status)
        val seen = mutableListOf<Unit>()
        val job = launch { session.changes.toList(seen) }
        advanceUntilIdle()

        status.value = DeviceControlStatus.Ready
        advanceUntilIdle()

        assertEquals(1, seen.size)
        assertTrue(session.canTakeControl())
        job.cancel()
    }
}
