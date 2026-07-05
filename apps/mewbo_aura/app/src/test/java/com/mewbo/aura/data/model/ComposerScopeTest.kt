package com.mewbo.aura.data.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ComposerScopeTest {

    private val configProject = ProjectSummary(name = "Assistant", source = "config")
    private val managedProject = ProjectSummary(name = "scratch-worktree", source = "managed", projectId = "abc123")
    private val toolA = ToolSummary(toolId = "mcp:gitea:issue_read", name = "issue_read", kind = "mcp", enabled = true, server = "gitea")
    private val toolB = ToolSummary(toolId = "mcp:gitea:pr_write", name = "pr_write", kind = "mcp", enabled = true, server = "gitea")
    private val toolC = ToolSummary(toolId = "mcp:langfuse:fetch_trace", name = "fetch_trace", kind = "mcp", enabled = false, server = "langfuse")

    // --- project key mapping (config vs managed) ---

    @Test
    fun `config project's context key is its bare name`() {
        assertEquals("Assistant", configProject.contextKey)
    }

    @Test
    fun `managed project's context key is prefixed with managed colon id`() {
        assertEquals("managed:abc123", managedProject.contextKey)
    }

    @Test
    fun `projectDisplayName falls back to Temporary when nothing is selected`() {
        val scope = ComposerScope(projects = listOf(configProject, managedProject), selectedProjectKey = null)
        assertEquals("Temporary", scope.projectDisplayName)
    }

    @Test
    fun `projectDisplayName resolves the selected managed project's name via its context key`() {
        val scope = ComposerScope(projects = listOf(configProject, managedProject), selectedProjectKey = "managed:abc123")
        assertEquals("scratch-worktree", scope.projectDisplayName)
    }

    @Test
    fun `projectDisplayName degrades to the raw key while the catalog hasn't loaded yet`() {
        // Revisit-session race (Gitea #185 P6): commit 3add535 hydrates selectedProjectKey before
        // refreshComposerScope resolves the catalog - a non-null selection must never read "Temporary".
        val scope = ComposerScope(projects = null, selectedProjectKey = "Assistant")
        assertEquals("Assistant", scope.projectDisplayName)
    }

    @Test
    fun `projectDisplayName degrades to the raw key when the loaded catalog does not contain it`() {
        val scope = ComposerScope(projects = listOf(configProject), selectedProjectKey = "managed:gone")
        assertEquals("managed:gone", scope.projectDisplayName)
    }

    // --- mcp_tools omitted-when-untouched vs narrowed ---

    @Test
    fun `untouched tool selection omits mcp_tools from context`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC), activeToolIds = null)
        assertFalse(scope.toolsNarrowed)
        assertNull(scope.mcpToolsForContext())
        assertEquals("All tools", scope.toolsSummaryLabel)
    }

    @Test
    fun `toggling one tool off narrows the selection and sends the explicit active set`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC)).toggleTool(toolA.toolId)

        assertTrue(scope.toolsNarrowed)
        assertEquals(setOf(toolB.toolId), scope.mcpToolsForContext()?.toSet())
        assertEquals("1 of 3 on", scope.toolsSummaryLabel)
    }

    @Test
    fun `toggling back to the exact default set un-narrows again`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC))
            .toggleTool(toolA.toolId)
            .toggleTool(toolA.toolId)

        assertFalse(scope.toolsNarrowed)
        assertNull(scope.mcpToolsForContext())
    }

    @Test
    fun `a tool disabled by default is off from the start without counting as narrowed`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC))

        assertFalse(scope.isToolActive(toolC.toolId))
        assertFalse(scope.toolsNarrowed)
        assertNull(scope.mcpToolsForContext())
    }

    @Test
    fun `explicitly enabling a default-off tool narrows the selection`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC)).toggleTool(toolC.toolId)

        assertTrue(scope.toolsNarrowed)
        assertEquals(setOf(toolA.toolId, toolB.toolId, toolC.toolId), scope.mcpToolsForContext()?.toSet())
    }

    // --- activeToolCount (pre-session scope indicator, Gitea #178 W1-B) ---

    @Test
    fun `activeToolCount is null while the tools catalog hasn't loaded`() {
        val scope = ComposerScope(tools = null)
        assertNull(scope.activeToolCount)
    }

    @Test
    fun `activeToolCount is the catalog default count when untouched`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC))
        assertEquals(2, scope.activeToolCount)
    }

    @Test
    fun `activeToolCount reflects a narrowed selection`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC)).toggleTool(toolA.toolId)
        assertEquals(1, scope.activeToolCount)
    }

    // --- setServerTools (toggle-sheet per-server bulk action) ---

    @Test
    fun `setServerTools active=false materializes the default set then removes the given ids`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC))
            .setServerTools(listOf(toolA.toolId, toolB.toolId), active = false)

        assertTrue(scope.toolsNarrowed)
        assertEquals(emptySet<String>(), scope.mcpToolsForContext()?.toSet())
    }

    @Test
    fun `setServerTools active=true turns on every given id on top of the resolved set`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC))
            .toggleTool(toolA.toolId) // narrow first: {toolB}
            .setServerTools(listOf(toolA.toolId, toolC.toolId), active = true)

        assertTrue(scope.toolsNarrowed)
        assertEquals(setOf(toolA.toolId, toolB.toolId, toolC.toolId), scope.mcpToolsForContext()?.toSet())
    }

    @Test
    fun `setServerTools turning a whole group off then back on returns to the catalog default`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC))
            .setServerTools(listOf(toolA.toolId, toolB.toolId), active = false)
            .setServerTools(listOf(toolA.toolId, toolB.toolId), active = true)

        assertFalse(scope.toolsNarrowed)
        assertNull(scope.mcpToolsForContext())
    }

    // --- activeCountFor (per-server "N of M on" caption) ---

    @Test
    fun `activeCountFor counts only tools in the given group at their catalog default`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC))
        assertEquals(2 to 2, scope.activeCountFor(toolA.groupKey))
        assertEquals(0 to 1, scope.activeCountFor(toolC.groupKey))
    }

    @Test
    fun `activeCountFor reflects a narrowed selection within the group`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC)).toggleTool(toolA.toolId)
        assertEquals(1 to 2, scope.activeCountFor(toolA.groupKey))
    }

    @Test
    fun `activeCountFor is zero-of-zero for an unknown group key`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC))
        assertEquals(0 to 0, scope.activeCountFor("unknown-server"))
    }
}
