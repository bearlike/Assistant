package com.mewbo.aura.data.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The ONE invariant that keeps the settings toggles honest: every shipped tool
 * is toggleable exactly once, and no group lists a tool that doesn't ship. Without this, adding a
 * tenth `device_*` tool to [DeviceToolCatalog.ALL] would leave it silently un-toggleable (always on,
 * no settings row), and deleting one would leave a dead switch that gates nothing. */
class DeviceToolTogglesTest {

    private val toggledIds: List<String> = DeviceToolToggles.GROUPS.flatMap { it.toggles }.map { it.toolId }

    @Test
    fun `the toggle groups cover exactly the shipped catalog, each tool once`() {
        assertEquals(DeviceToolCatalog.ALL.map { it.toolId }.toSet(), toggledIds.toSet())
    }

    @Test
    fun `no tool id appears in more than one toggle`() {
        assertEquals(toggledIds.size, toggledIds.toSet().size)
        assertEquals(DeviceToolCatalog.ALL.size, toggledIds.size)
    }

    @Test
    fun `every toggle carries a non-blank human label`() {
        DeviceToolToggles.GROUPS.forEach { group ->
            assertTrue("group title blank", group.title.isNotBlank())
            group.toggles.forEach { assertTrue("${it.toolId} label blank", it.label.isNotBlank()) }
        }
    }
}
