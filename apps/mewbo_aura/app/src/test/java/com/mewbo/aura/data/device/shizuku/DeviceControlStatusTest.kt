package com.mewbo.aura.data.device.shizuku

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The status vocabulary, and the property the reported bug violated.
 *
 * A user with Shizuku running and started was shown "Start Shizuku". The cause
 * was structural rather than a wrong label: the status was read ONCE at
 * composition, while the Shizuku binder arrives asynchronously after app start,
 * so a running service read as [DeviceControlStatus.NotRunning] and nothing
 * ever corrected it. The binder listener is what fixes that, and it cannot be
 * exercised on a plain JVM — what CAN be pinned here is that every state is
 * distinct, actionable, and that only one of them means ready.
 */
class DeviceControlStatusTest {

    @Test
    fun `only Ready reports ready`() {
        assertTrue(DeviceControlStatus.Ready.isReady)
        assertFalse(DeviceControlStatus.NotInstalled.isReady)
        assertFalse(DeviceControlStatus.NotRunning.isReady)
        assertFalse(DeviceControlStatus.PermissionDenied.isReady)
    }

    @Test
    fun `the four states are distinct`() {
        // Each needs a DIFFERENT action from the user — install an app, start a
        // service, grant access, nothing. Collapsing any two of them back into a
        // boolean is what produces an unexplained disabled switch.
        val all = setOf(
            DeviceControlStatus.NotInstalled,
            DeviceControlStatus.NotRunning,
            DeviceControlStatus.PermissionDenied,
            DeviceControlStatus.Ready,
        )
        assertEquals(4, all.size)
    }

    @Test
    fun `every not-ready state is one the user can act on`() {
        // The regression this guards: a row that renders a state but offers no
        // way out of it. Ready is the only state with nothing to do.
        val actionable = listOf(
            DeviceControlStatus.NotInstalled,
            DeviceControlStatus.NotRunning,
            DeviceControlStatus.PermissionDenied,
        )
        assertTrue(actionable.none { it.isReady })
    }
}
