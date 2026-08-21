package com.mewbo.aura.data.device.shizuku

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The app-op grant's decision, with no Shizuku binder and no Android framework
 * anywhere — the reason all three collaborators are narrow seams.
 *
 * The claim these pin is not "the command was formed correctly"; it is that a
 * command RUNNING is never mistaken for the permission being granted. That
 * distinction is the whole point of the class, and it is exactly the one a test
 * asserting an exit code would lose.
 */
class ShizukuOverlayGrantTest {

    /** Records every command it is handed, so "no command ran" is assertable
     * rather than inferred from an outcome. */
    private class RecordingShell(private val output: String? = "") : DeviceShellRunner {
        val commands = mutableListOf<String>()

        override suspend fun run(command: String, timeoutMs: Int): String? {
            commands += command
            return output
        }
    }

    /** The overlay read, scripted so it can FLIP between the two reads `grant()`
     * makes — which is the only way to model "the write took". */
    private class ScriptedOverlayState(private vararg val reads: Boolean) : OverlayPermissionState {
        private var next = 0

        override fun isGranted(): Boolean = reads[minOf(next++, reads.lastIndex)]
    }

    private fun grant(
        status: DeviceControlStatus = DeviceControlStatus.Ready,
        shell: DeviceShellRunner = RecordingShell(),
        overlay: OverlayPermissionState,
    ) = ShizukuOverlayGrant(
        packageName = "com.mewbo.aura",
        statusSource = DeviceControlStatusSource { MutableStateFlow(status) },
        shell = shell,
        overlayState = overlay,
    )

    @Test
    fun `an already-granted permission short-circuits without running anything`() = runTest {
        val shell = RecordingShell()

        val outcome = grant(shell = shell, overlay = ScriptedOverlayState(true)).grant()

        assertSame(OverlayGrantOutcome.AlreadyGranted, outcome)
        assertTrue(outcome.granted)
        // The load-bearing half: a shell round trip for a permission already
        // held is pure cost on a path a settings row can tap repeatedly.
        assertEquals(emptyList<String>(), shell.commands)
    }

    @Test
    fun `a write that takes reports granted, and writes the app-op not the permission`() = runTest {
        val shell = RecordingShell()

        val outcome = grant(shell = shell, overlay = ScriptedOverlayState(false, true)).grant()

        assertSame(OverlayGrantOutcome.Granted, outcome)
        assertTrue(outcome.granted)
        assertEquals(listOf("cmd appops set com.mewbo.aura SYSTEM_ALERT_WINDOW allow"), shell.commands)
    }

    /**
     * The outcome an exit-code check would report as success. `appops` exits 0
     * for a command it merely parsed, so the second read is the only evidence
     * there is — the same rule as a failed `screencap` that still creates its
     * output file.
     */
    @Test
    fun `a command that ran while the permission stays false is its own outcome`() = runTest {
        val shell = RecordingShell(output = "")

        val outcome = grant(shell = shell, overlay = ScriptedOverlayState(false, false)).grant()

        assertEquals(OverlayGrantOutcome.StillDenied(""), outcome)
        assertFalse(outcome.granted)
        assertEquals(1, shell.commands.size)
    }

    /** The combined output is kept because it is the only place a real cause is
     * ever stated — an outcome that dropped it leaves the user with nothing. */
    @Test
    fun `a refused write keeps the command output`() = runTest {
        val shell = RecordingShell(output = "Error: Unknown operation string: SYSTEM_ALERT_WINDOW")

        val outcome = grant(shell = shell, overlay = ScriptedOverlayState(false, false)).grant()

        assertEquals(
            OverlayGrantOutcome.StillDenied("Error: Unknown operation string: SYSTEM_ALERT_WINDOW"),
            outcome,
        )
    }

    @Test
    fun `every not-ready Shizuku state refuses by name, and runs nothing`() = runTest {
        val notReady = listOf(
            DeviceControlStatus.NotInstalled,
            DeviceControlStatus.NotRunning,
            DeviceControlStatus.PermissionDenied,
        )

        for (status in notReady) {
            val shell = RecordingShell()

            val outcome = grant(status, shell, ScriptedOverlayState(false)).grant()

            // WHICH state, not merely "unavailable": the remedy differs for
            // every one of them and a caller must be able to branch on it.
            assertEquals(OverlayGrantOutcome.ShizukuUnavailable(status), outcome)
            assertFalse(outcome.granted)
            assertEquals(emptyList<String>(), shell.commands)
            assertTrue(outcome.message.isNotBlank())
        }
    }

    /**
     * A status saying Shizuku WOULD allow a bind is not a bind. Reported as a
     * down service because that is the user's identical remedy — the precedent
     * `DeviceControlSession.start` set for the same fact.
     */
    @Test
    fun `a bind that never happens refuses rather than reporting a write`() = runTest {
        val overlay = ScriptedOverlayState(false)

        val outcome = grant(shell = { _, _ -> null }, overlay = overlay).grant()

        assertEquals(OverlayGrantOutcome.ShizukuUnavailable(DeviceControlStatus.NotRunning), outcome)
        assertFalse(outcome.granted)
    }

    /** Every arm has to be renderable verbatim by the caller — refusals and the hand-off included.
     * A silent arm is indistinguishable from a broken button, which is why none may be blank. */
    @Test
    fun `every outcome carries a message and a distinct code`() {
        val outcomes = listOf(
            OverlayGrantOutcome.SentToSystemSettings,
            OverlayGrantOutcome.AlreadyGranted,
            OverlayGrantOutcome.Granted,
            OverlayGrantOutcome.ShizukuUnavailable(DeviceControlStatus.NotInstalled),
            OverlayGrantOutcome.StillDenied(null),
        )

        assertEquals(outcomes.size, outcomes.map { it.code }.toSet().size)
        assertTrue(outcomes.all { it.message.isNotBlank() })
        // Only the two success arms may claim the call granted anything.
        assertEquals(2, outcomes.count { it.granted })
        assertNull(OverlayGrantOutcome.StillDenied(null).output)
    }
}
