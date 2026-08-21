package com.mewbo.aura.data.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The ONE invariant that keeps the settings toggles honest: every shipped tool
 * is toggleable exactly once, and no group lists a tool that doesn't ship. Without this, adding a
 * tenth `device_*` tool to [DeviceToolCatalog.ALL] would leave it silently un-toggleable (always on,
 * no settings row), and deleting one would leave a dead switch that gates nothing.
 *
 * [DeviceToolCatalog.LIFECYCLE_TOOL_IDS] are the ONE exemption, and it is asserted rather than
 * assumed: a switch for `device_control_start` would let a user disable the gate while leaving the
 * tools it guards enabled, which reads backwards. They ride the screen-control opt-in instead, so
 * they are covered by those switches without owning any. */
class DeviceToolTogglesTest {

    private val toggledIds: List<String> = DeviceToolToggles.GROUPS.flatMap { it.toggles }.map { it.toolId }

    private val expectedToggleableIds: Set<String> =
        DeviceToolCatalog.ALL.map { it.toolId }.toSet() - DeviceToolCatalog.LIFECYCLE_TOOL_IDS

    @Test
    fun `the toggle groups cover exactly the shipped catalog, each tool once`() {
        assertEquals(expectedToggleableIds, toggledIds.toSet())
    }

    @Test
    fun `no tool id appears in more than one toggle`() {
        assertEquals(toggledIds.size, toggledIds.toSet().size)
        assertEquals(expectedToggleableIds.size, toggledIds.size)
    }

    @Test
    fun `the lifecycle pair is the only exemption, and it is deliberate`() {
        // Pinned so a THIRD un-toggleable tool cannot arrive silently: the
        // exemption is an argued one, not a hole in the invariant above.
        assertEquals(
            DeviceToolCatalog.LIFECYCLE_TOOL_IDS,
            DeviceToolCatalog.ALL.map { it.toolId }.toSet() - toggledIds.toSet(),
        )
    }

    @Test
    fun `every toggle carries a non-blank human label`() {
        DeviceToolToggles.GROUPS.forEach { group ->
            assertTrue("group title blank", group.title.isNotBlank())
            group.toggles.forEach { assertTrue("${it.toolId} label blank", it.label.isNotBlank()) }
        }
    }
}
