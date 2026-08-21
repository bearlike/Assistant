package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.DeviceControlGate
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The capability and the tools must be decided by ONE predicate.
 *
 * They were not, and that was the defect: the capability header was computed
 * from the user's toggles alone while the tool list additionally required
 * Shizuku to be live. A device with the toggles on and the service down
 * therefore advertised the `device_control` capability — which activates the
 * playbook skill server-side — while sending none of the tools it describes.
 * The agent activated the skill, searched for `device_ui`, found nothing, and
 * had to walk the failure back to a user watching their phone.
 */
class DeviceControlAdvertiseTest {

    private fun catalog(disabled: Set<String>, shizukuReady: Boolean) = DeviceToolCatalog(
        DevicePermissionChecker { true },
        DeviceToolGate { disabled },
        DeviceControlGate { shizukuReady },
    )

    @Test
    fun `toggles on but Shizuku down advertises NOTHING`() = runTest {
        // The exact reported state. The capability must be withheld with the
        // tools, not shipped alone.
        val c = catalog(disabled = emptySet(), shizukuReady = false)

        assertFalse(c.advertisesDeviceControl())
        assertTrue(c.availableTools().none { it.toolId in DeviceToolCatalog.CONTROL_TOOL_IDS })
    }

    @Test
    fun `toggles on and Shizuku ready advertises BOTH`() = runTest {
        val c = catalog(disabled = emptySet(), shizukuReady = true)

        assertTrue(c.advertisesDeviceControl())
        assertTrue(c.availableTools().any { it.toolId == "device_ui" })
    }

    @Test
    fun `Shizuku ready but every control tool switched off advertises nothing`() = runTest {
        // The other direction: a playbook for tools the user has turned off is
        // pure context cost.
        val c = catalog(disabled = DeviceToolCatalog.CONTROL_TOOL_IDS, shizukuReady = true)

        assertFalse(c.advertisesDeviceControl())
    }

    @Test
    fun `one control tool left on is enough to advertise`() = runTest {
        val c = catalog(disabled = setOf("device_action", "device_shell"), shizukuReady = true)

        assertTrue(c.advertisesDeviceControl())
    }

    @Test
    fun `the capability tracks the tool list EXACTLY, in every combination`() = runTest {
        // The invariant that makes the divergence structurally impossible:
        // advertise iff at least one control tool is actually on the wire.
        for (ready in listOf(true, false)) {
            for (disabled in listOf(
                emptySet(),
                setOf("device_ui"),
                setOf("device_ui", "device_action"),
                DeviceToolCatalog.CONTROL_TOOL_IDS,
            )) {
                val c = catalog(disabled, ready)
                val toolsPresent =
                    c.availableTools().any { it.toolId in DeviceToolCatalog.CONTROL_TOOL_IDS }
                org.junit.Assert.assertEquals(
                    "ready=$ready disabled=$disabled",
                    toolsPresent,
                    c.advertisesDeviceControl(),
                )
            }
        }
    }
}
