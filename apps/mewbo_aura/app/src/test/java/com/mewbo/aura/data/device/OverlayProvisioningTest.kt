package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.device.shizuku.OverlayGrantOutcome
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Which route a device leads with, and that each route takes only its own — on a plain JVM with no
 * Activity, no `Context` and no Shizuku binder, which is what carrying both I/O legs as a method
 * ARG buys.
 *
 * The claim worth pinning is not that a mapping function returns the right constant; it is that
 * **no route can fire the other one's leg**. A television reaching the system intent is a button
 * that silently does nothing, and that failure is invisible on every device a developer holds.
 */
class OverlayProvisioningTest {

    /** Records which leg ran, so "the other one did NOT fire" is assertable rather than inferred
     * from a return value both arms could produce. */
    private class RecordingRoutes(
        private val grantOutcome: OverlayGrantOutcome = OverlayGrantOutcome.Granted,
    ) : OverlayProvisioning.Routes {
        var systemScreenOpened = 0
        var shizukuGrants = 0

        override fun openSystemOverlayScreen() {
            systemScreenOpened++
        }

        override suspend fun grantThroughShizuku(): OverlayGrantOutcome {
            shizukuGrants++
            return grantOutcome
        }
    }

    @Test
    fun `a handheld leads with the system screen and a television with the app-op`() {
        assertSame(
            OverlayProvisioning.SystemSettingsScreen,
            OverlayProvisioning.primaryFor(DeviceShape.Handheld),
        )
        assertSame(
            OverlayProvisioning.ShizukuAppOp,
            OverlayProvisioning.primaryFor(DeviceShape.Television),
        )
    }

    /** Not a restatement of the mapping: it pins that the selector reads the SHAPE'S OWN member,
     * so a shape that answers the question differently is routed differently with no edit here. */
    @Test
    fun `the selector reads hasOverlayPermissionScreen, not the shape's identity`() {
        for (shape in listOf(DeviceShape.Handheld, DeviceShape.Television)) {
            val expected = if (shape.hasOverlayPermissionScreen) {
                OverlayProvisioning.SystemSettingsScreen
            } else {
                OverlayProvisioning.ShizukuAppOp
            }
            assertSame(expected, OverlayProvisioning.primaryFor(shape))
        }
    }

    @Test
    fun `the system route opens the screen, writes no app-op, and claims nothing`() = runTest {
        val routes = RecordingRoutes()

        val outcome = OverlayProvisioning.SystemSettingsScreen.provision(routes)

        assertSame(OverlayGrantOutcome.SentToSystemSettings, outcome)
        assertEquals(1, routes.systemScreenOpened)
        // The load-bearing half. A hand-off that also wrote the app-op would grant the permission
        // on a device whose user never confirmed anything on the screen they were sent to.
        assertEquals(0, routes.shizukuGrants)
        // Claims nothing: it granted nothing, and the resume re-read is what eventually answers.
        assertFalse(outcome.granted)
        assertTrue(outcome.message.isNotBlank())
    }

    @Test
    fun `the app-op route grants, never opens a screen that may not exist`() = runTest {
        val routes = RecordingRoutes()

        val outcome = OverlayProvisioning.ShizukuAppOp.provision(routes)

        assertSame(OverlayGrantOutcome.Granted, outcome)
        assertEquals(1, routes.shizukuGrants)
        assertEquals(0, routes.systemScreenOpened)
    }

    /** The arm relays the grant's own answer rather than reducing it — a refusal naming Shizuku's
     * state is the whole reason the outcome is a union and not a boolean. */
    @Test
    fun `the app-op route relays a refusal verbatim`() = runTest {
        val refusal = OverlayGrantOutcome.ShizukuUnavailable(DeviceControlStatus.NotInstalled)
        val routes = RecordingRoutes(grantOutcome = refusal)

        val outcome = OverlayProvisioning.ShizukuAppOp.provision(routes)

        assertSame(refusal, outcome)
        assertNotNull(outcome.message)
    }

    /**
     * Primary is not the same question as available: the app-op stays offered on a handheld, whose
     * settings screen can be missing for its own reasons. The reverse would be a second control
     * that opens nothing.
     */
    @Test
    fun `the system route offers the app-op alongside it, and the app-op offers nothing`() {
        assertSame(OverlayProvisioning.ShizukuAppOp, OverlayProvisioning.SystemSettingsScreen.alternative)
        assertNull(OverlayProvisioning.ShizukuAppOp.alternative)
    }

    @Test
    fun `both routes carry a caption and an action label for the row that offers them`() {
        for (route in listOf(OverlayProvisioning.SystemSettingsScreen, OverlayProvisioning.ShizukuAppOp)) {
            assertTrue(route.caption.isNotBlank())
            assertTrue(route.actionLabel.isNotBlank())
        }
        // Different mechanisms, so a secondary control cannot read identically to the primary one.
        assertTrue(
            OverlayProvisioning.SystemSettingsScreen.actionLabel !=
                OverlayProvisioning.ShizukuAppOp.actionLabel,
        )
    }
}
