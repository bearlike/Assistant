package com.mewbo.aura.data.repo

import com.mewbo.aura.data.device.DeviceToolDefinition
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** [buildSessionContext] is the ONE seam both [SessionRepository.createSession] and
 * [RunRepository.sendQuery] build `context` through - this is the wire-level counterpart to
 * [com.mewbo.aura.data.model.ComposerScopeTest] (which tests the domain-level derivation that feeds
 * these plain params in). */
class SessionContextTest {

    @Test
    fun `always carries the client marker, even with nothing else set`() {
        val context = buildSessionContext(model = null, project = null, mcpTools = null)

        assertEquals("aura-android", context["client"]?.jsonPrimitive?.content)
        assertFalse(context.containsKey("model"))
        assertFalse(context.containsKey("project"))
        assertFalse(context.containsKey("mcp_tools"))
    }

    @Test
    fun `blank model and project are omitted, not sent as empty strings`() {
        val context = buildSessionContext(model = "  ", project = "", mcpTools = null)

        assertFalse(context.containsKey("model"))
        assertFalse(context.containsKey("project"))
    }

    @Test
    fun `a real model and project are included verbatim`() {
        val context = buildSessionContext(model = "claude-sonnet-5", project = "managed:abc123", mcpTools = null)

        assertEquals("claude-sonnet-5", context["model"]?.jsonPrimitive?.content)
        assertEquals("managed:abc123", context["project"]?.jsonPrimitive?.content)
    }

    @Test
    fun `an empty mcp_tools list is omitted the same as null - untouched means untouched`() {
        val context = buildSessionContext(model = null, project = null, mcpTools = emptyList())

        assertFalse(context.containsKey("mcp_tools"))
    }

    @Test
    fun `a narrowed mcp_tools list is sent verbatim`() {
        val context = buildSessionContext(model = null, project = null, mcpTools = listOf("mcp:gitea:issue_read", "mcp:gitea:pr_write"))

        val ids = context["mcp_tools"]?.jsonArray?.map { it.jsonPrimitive.content }
        assertEquals(listOf("mcp:gitea:issue_read", "mcp:gitea:pr_write"), ids)
        assertTrue(context.containsKey("mcp_tools"))
    }

    @Test
    fun `device_tools is omitted when null or empty, same as mcp_tools`() {
        assertFalse(buildSessionContext(model = null, project = null, mcpTools = null, deviceTools = null).containsKey("device_tools"))
        assertFalse(buildSessionContext(model = null, project = null, mcpTools = null, deviceTools = emptyList()).containsKey("device_tools"))
    }

    @Test
    fun `a non-empty device_tools list is sent as tool_id-description-parameters entries`() {
        val definition = DeviceToolDefinition(
            toolId = "device_get_time",
            description = "Get the device's current time.",
            parameters = buildJsonObject { put("type", "object") },
        )

        val context = buildSessionContext(model = null, project = null, mcpTools = null, deviceTools = listOf(definition))

        val entries = context["device_tools"]?.jsonArray
        assertEquals(1, entries?.size)
        val entry = entries!!.first().jsonObject
        assertEquals("device_get_time", entry["tool_id"]?.jsonPrimitive?.content)
        assertEquals("Get the device's current time.", entry["description"]?.jsonPrimitive?.content)
        assertEquals("object", entry["parameters"]?.jsonObject?.get("type")?.jsonPrimitive?.content)
        // requiredPermission is a client-only gating concept and must never cross the wire.
        assertFalse(entry.containsKey("requiredPermission"))
    }
}
