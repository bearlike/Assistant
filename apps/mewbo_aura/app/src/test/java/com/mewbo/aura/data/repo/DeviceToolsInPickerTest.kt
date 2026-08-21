package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.ToolDto
import com.mewbo.aura.data.api.ToolsResponseDto
import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.device.DeviceToolCatalog
import com.mewbo.aura.data.device.DeviceToolGate
import com.mewbo.aura.data.device.DeviceControlSession
import com.mewbo.aura.data.device.shizuku.DeviceControlBinder
import com.mewbo.aura.data.device.shizuku.DeviceControlGate
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.device.shizuku.DeviceControlStatusSource
import com.mewbo.aura.data.model.ComposerScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mockito.Mockito.mock
import org.mockito.Mockito.`when`

/**
 * Device tools must reach the tool picker.
 *
 * They are declared by the CLIENT on each query, so they appear in no
 * `GET api/tools` response — and consequently appeared in no group of the
 * picker. The one tool family that acts on the user's own phone, including a
 * shell at shell UID, was the only family absent from the surface built for
 * seeing and controlling what the agent can reach.
 */
class DeviceToolsInPickerTest {

    private fun repo(
        serverTools: List<ToolDto> = emptyList(),
        disabled: Set<String> = emptySet(),
        shizukuReady: Boolean = true,
        permissionsGranted: Boolean = true,
    ) = SessionScopeRepository(
        mock(AuraApi::class.java).also {
            runBlocking { `when`(it.getTools(null)).thenReturn(ToolsResponseDto(serverTools)) }
        },
        DeviceToolCatalog(
            DevicePermissionChecker { permissionsGranted },
            DeviceToolGate { disabled },
            DeviceControlGate { shizukuReady },
        ),
        DeviceControlSession(
            DeviceControlStatusSource {
                MutableStateFlow(
                    if (shizukuReady) DeviceControlStatus.Ready else DeviceControlStatus.NotRunning,
                )
            },
            DeviceControlBinder { shizukuReady },
            kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Unconfined),
        ),
    )

    @Test
    fun `device tools appear in the picker list, tagged with the device scope`() = runTest {
        val tools = repo().tools()!!

        val device = tools.filter { it.scope == ComposerScope.FACET_DEVICE }
        assertTrue("expected device rows", device.isNotEmpty())
        assertTrue(device.any { it.toolId == "device_ui" })
        assertTrue(device.any { it.toolId == "device_get_battery" })
    }

    @Test
    fun `the device scope is a real facet, ordered ahead of system`() = runTest {
        // Ordering is the user-facing half: a tool acting on their own phone is
        // the one they most need to see.
        assertTrue(ComposerScope.FACET_DEVICE in ComposerScope.FACET_ORDER)
        assertTrue(
            ComposerScope.FACET_ORDER.indexOf(ComposerScope.FACET_DEVICE) <
                ComposerScope.FACET_ORDER.indexOf("system"),
        )
    }

    @Test
    fun `the picker shows what the session ADVERTISES, not what the build ships`() = runTest {
        // A tool switched off in Settings is not sent to the agent, so showing it
        // in the picker as available would misdescribe the session.
        val tools = repo(disabled = setOf("device_ui")).tools()!!

        assertTrue(tools.none { it.toolId == "device_ui" })
        assertTrue(tools.any { it.toolId == "device_action" })
    }

    @Test
    fun `no screen-control rows appear while Shizuku is down`() = runTest {
        val tools = repo(shizukuReady = false).tools()!!

        assertTrue(tools.none { it.toolId in DeviceToolCatalog.CONTROL_TOOL_IDS })
        // The permission-free tools are unaffected — the gate is per-family.
        assertTrue(tools.any { it.toolId == "device_get_time" })
    }

    @Test
    fun `server tools and device tools coexist, neither displacing the other`() = runTest {
        val tools = repo(
            serverTools = listOf(
                ToolDto(toolId = "mcp_x", name = "x", kind = "mcp", server = "srv", scope = "project"),
            ),
        ).tools()!!

        assertTrue(tools.any { it.toolId == "mcp_x" && it.scope == "project" })
        assertTrue(tools.any { it.scope == ComposerScope.FACET_DEVICE })
    }

    @Test
    fun `a device row is grouped under one server heading`() = runTest {
        val device = repo().tools()!!.filter { it.scope == ComposerScope.FACET_DEVICE }

        assertEquals(setOf("This device"), device.map { it.groupKey }.toSet())
        assertFalse("names should be readable, not raw ids", device.any { it.name.startsWith("device_") })
    }
}
