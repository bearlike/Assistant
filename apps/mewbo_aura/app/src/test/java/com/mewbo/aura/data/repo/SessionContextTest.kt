package com.mewbo.aura.data.repo

import com.mewbo.aura.data.device.DeviceToolDefinition
import com.mewbo.aura.data.model.ComposerScope
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
    fun `the auto-select sentinel is sent verbatim, like any other project key`() {
        // "auto" is a RESERVED value, not a project - but it is not special-cased HERE: it rides
        // the ordinary `project` field so the resolver on the far side owns the whole meaning of
        // it. A client that dropped it (or translated it into some other shape) would silently
        // start every auto session in a temp dir with no way for the model to be asked to pick.
        val context = buildSessionContext(model = null, project = ComposerScope.AUTO_PROJECT_KEY, mcpTools = null)

        assertEquals("auto", context["project"]?.jsonPrimitive?.content)
    }

    @Test
    fun `the sentinel this client sends is the one the resolver reserves`() {
        // A second spelling of the sentinel on either side of the wire is indistinguishable from a
        // project literally named "auto" - which is why core declares it once (project_catalog's
        // AUTO_PROJECT) and this client declares it once.
        assertEquals("auto", ComposerScope.AUTO_PROJECT_KEY)
    }

    // ── the mcp_tools tri-state ─────────────────────────────────────────────
    // All three arms are pinned together deliberately. Only the non-empty case was covered before,
    // and that is precisely what let `isNullOrEmpty()` sit here: it is correct on two of the three
    // states, and the one it gets wrong (empty) fails OPEN, so nothing downstream reports it.

    @Test
    fun `a null mcp_tools list omits the key - no ceiling, the backend binds every tool`() {
        val context = buildSessionContext(model = null, project = null, mcpTools = null)

        assertFalse(context.containsKey("mcp_tools"))
    }

    @Test
    fun `an EMPTY mcp_tools list is sent as an empty array, never omitted`() {
        // The whole defect: `isNullOrEmpty()` collapsed this into the null arm above, so a user who
        // switched every tool off transmitted "I have no preference" and the server re-bound the
        // entire MCP registry. Absence and a declared zero are opposite instructions on the wire
        // (`_extract_allowed_tools` preserves `[]` on purpose) and the client must be able to say
        // both.
        val context = buildSessionContext(model = null, project = null, mcpTools = emptyList())

        assertTrue(context.containsKey("mcp_tools"))
        assertEquals(0, context["mcp_tools"]?.jsonArray?.size)
    }

    @Test
    fun `a narrowed mcp_tools list is sent verbatim`() {
        val context = buildSessionContext(model = null, project = null, mcpTools = listOf("mcp:gitea:issue_read", "mcp:gitea:pr_write"))

        val ids = context["mcp_tools"]?.jsonArray?.map { it.jsonPrimitive.content }
        assertEquals(listOf("mcp:gitea:issue_read", "mcp:gitea:pr_write"), ids)
        assertTrue(context.containsKey("mcp_tools"))
    }

    // ── the device_tools tri-state ──────────────────────────────────────────
    // Same SHAPE as mcp_tools, different meaning for the absent arm, so it is pinned separately
    // rather than by analogy: `DeviceToolBinding.declaration_for` treats a missing key as SILENCE
    // and falls back to the session's newest context event that carries one. An omitted empty list
    // therefore leaves a previously-declared set bound - a revoked device tool stays live.

    @Test
    fun `a null device_tools list omits the key - this call site declares nothing`() {
        // createSession's arm: session creation is not where device tools are enumerated, so it
        // says nothing and lets the /query that follows declare them.
        assertFalse(buildSessionContext(model = null, project = null, mcpTools = null, deviceTools = null).containsKey("device_tools"))
    }

    @Test
    fun `an EMPTY device_tools list is sent as an empty array, never omitted`() {
        val context = buildSessionContext(model = null, project = null, mcpTools = null, deviceTools = emptyList())

        assertTrue(context.containsKey("device_tools"))
        assertEquals(0, context["device_tools"]?.jsonArray?.size)
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
