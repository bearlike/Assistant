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
        // Revisit-session race: a revisited session hydrates selectedProjectKey before
        // refreshComposerScope resolves the catalog - a non-null selection must never read "Temporary".
        val scope = ComposerScope(projects = null, selectedProjectKey = "Assistant")
        assertEquals("Assistant", scope.projectDisplayName)
    }

    @Test
    fun `projectDisplayName degrades to the raw key when the loaded catalog does not contain it`() {
        val scope = ComposerScope(projects = listOf(configProject), selectedProjectKey = "managed:gone")
        assertEquals("managed:gone", scope.projectDisplayName)
    }

    // --- auto-select mode ---
    //
    // "auto" is a RESERVED key, not a project: it never appears in the catalog and resolves to no
    // directory until the model calls switch_project. Everything below exists so it can never be
    // mistaken for either a real project (it would degrade to the raw "auto") or for Temporary
    // (which is a cwd nobody will move off).

    @Test
    fun `projectDisplayName labels the auto sentinel rather than degrading to the raw key`() {
        val scope = ComposerScope(projects = listOf(configProject), selectedProjectKey = ComposerScope.AUTO_PROJECT_KEY)
        assertEquals("Auto", scope.projectDisplayName)
        assertTrue(scope.isAutoProject)
    }

    @Test
    fun `auto is not Temporary - the two are different modes and must not collapse`() {
        assertFalse(ComposerScope(selectedProjectKey = null).isAutoProject)
        assertEquals("Temporary", ComposerScope(selectedProjectKey = null).projectDisplayName)
    }

    @Test
    fun `a real project named auto is out of reach by construction - the sentinel wins the key`() {
        // Documenting the accepted collision rather than defending against it: the resolver reserves
        // this key too, so a project could not be addressed by it either way.
        val scope = ComposerScope(
            projects = listOf(ProjectSummary(name = "auto", source = "config")),
            selectedProjectKey = "auto",
        )
        assertEquals("Auto", scope.projectDisplayName)
    }

    @Test
    fun `a tool-catalog fetch is UNSCOPED in auto mode - there is no project to scope it to yet`() {
        assertNull(ComposerScope(selectedProjectKey = ComposerScope.AUTO_PROJECT_KEY).toolScopeKey)
        assertNull(ComposerScope.toolScopeKeyOf(ComposerScope.AUTO_PROJECT_KEY))
    }

    @Test
    fun `every other project key scopes the tool catalog to itself, unchanged`() {
        assertEquals("Assistant", ComposerScope(selectedProjectKey = "Assistant").toolScopeKey)
        assertEquals("managed:abc123", ComposerScope.toolScopeKeyOf("managed:abc123"))
        assertNull(ComposerScope(selectedProjectKey = null).toolScopeKey)
    }

    // --- activeProjectLabel: the open-session top bar's "where am I working" readout ---

    @Test
    fun `activeProjectLabel is null for a session with no project, so the bar stays one line`() {
        assertNull(ComposerScope(selectedProjectKey = null).activeProjectLabel)
    }

    @Test
    fun `activeProjectLabel reads Auto until the model has picked, then the project it picked`() {
        val scope = ComposerScope(projects = listOf(configProject, managedProject))

        assertEquals("Auto", scope.copy(selectedProjectKey = ComposerScope.AUTO_PROJECT_KEY).activeProjectLabel)
        // What a switch_project's context event folds in mid-run (SessionEvent.adoptContextProject).
        assertEquals("scratch-worktree", scope.copy(selectedProjectKey = "managed:abc123").activeProjectLabel)
    }

    @Test
    fun `activeProjectLabel degrades to the raw key while the catalog hasn't loaded`() {
        // Same posture as projectDisplayName: never claim a project the catalog can't confirm, but
        // never claim there ISN'T one either - a switched-to key is a real fact about the session.
        assertEquals("acme/beacon", ComposerScope(projects = null, selectedProjectKey = "acme/beacon").activeProjectLabel)
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

    @Test
    fun `an explicit empty selection stays narrowed while the catalog hasn't loaded`() {
        // The hydration path: ChatViewModel.bind reads a session's persisted `mcp_tools: []` into
        // activeToolIds before refreshComposerScope has resolved the catalog. defaultActiveToolIds
        // derives FROM that catalog, so with tools == null it reads as the empty set - and an
        // un-guarded "equals the default" test would have compared equal, reported not-narrowed,
        // and re-widened the session to every tool on its next send.
        val scope = ComposerScope(tools = null, activeToolIds = emptySet())

        assertTrue(scope.toolsNarrowed)
        assertEquals(emptyList<String>(), scope.mcpToolsForContext())
    }

    @Test
    fun `a narrowed selection hydrated before the catalog is re-declared verbatim`() {
        val scope = ComposerScope(tools = null, activeToolIds = setOf(toolB.toolId))

        assertTrue(scope.toolsNarrowed)
        assertEquals(listOf(toolB.toolId), scope.mcpToolsForContext())
    }

    // --- activeToolCount (pre-session scope indicator) ---

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

    // --- provenance facets (scope row grouped counts, user directive) ---

    private fun scoped(id: String, scope: String?, enabled: Boolean = true) =
        ToolSummary(toolId = id, name = id, kind = "mcp", enabled = enabled, scope = scope)

    @Test
    fun `activeToolFacets groups active tools by scope in FACET_ORDER, non-zero only`() {
        // Input deliberately scrambled (plugin, system, project…) to prove the OUTPUT follows
        // FACET_ORDER, not arrival order.
        val scope = ComposerScope(
            tools = listOf(
                scoped("plug:a", "plugin"),
                scoped("sys:a", "system"),
                scoped("proj:a", "project"),
                scoped("proj:b", "project"),
                scoped("sys:b", "system"),
            ),
        )
        assertEquals(
            listOf(ToolFacet("project", 2), ToolFacet("system", 2), ToolFacet("plugin", 1)),
            scope.activeToolFacets(),
        )
    }

    @Test
    fun `builtin sorts after project-system-plugin`() {
        val scope = ComposerScope(
            tools = listOf(scoped("core:a", "builtin"), scoped("proj:a", "project"), scoped("sys:a", "system")),
        )
        assertEquals(listOf("project", "system", "builtin"), scope.activeToolFacets().map { it.scope })
    }

    @Test
    fun `toolsFacetSummary joins the faceted counts`() {
        val scope = ComposerScope(
            tools = listOf(scoped("proj:a", "project"), scoped("proj:b", "project"), scoped("sys:a", "system"), scoped("plug:a", "plugin")),
        )
        assertEquals("2 project · 1 system · 1 plugin", scope.toolsFacetSummary)
    }

    @Test
    fun `null and unrecognized scope collapse into a single other facet`() {
        val scope = ComposerScope(tools = listOf(scoped("a", null), scoped("b", "bogus")))
        assertEquals(listOf(ToolFacet("other", 2)), scope.activeToolFacets())
    }

    @Test
    fun `toolsFacetSummary falls back to the plain total when no active tool carries a recognized scope`() {
        // Older backend that omits scope entirely: never a lone, meaningless "2 other".
        val scope = ComposerScope(tools = listOf(scoped("a", null), scoped("b", null)))
        assertEquals("2 tools", scope.toolsFacetSummary)
    }

    @Test
    fun `an unscoped tool alongside a scoped one shows an other facet at the end`() {
        val scope = ComposerScope(tools = listOf(scoped("proj:a", "project"), scoped("x", null)))
        assertEquals("1 project · 1 other", scope.toolsFacetSummary)
    }

    @Test
    fun `narrowing drops a tool from its facet`() {
        val scope = ComposerScope(tools = listOf(scoped("proj:a", "project"), scoped("proj:b", "project"), scoped("sys:a", "system")))
            .toggleTool("proj:a")
        assertEquals(listOf(ToolFacet("project", 1), ToolFacet("system", 1)), scope.activeToolFacets())
        assertEquals("1 project · 1 system", scope.toolsFacetSummary)
    }

    @Test
    fun `a default-disabled tool is absent from the facets`() {
        val scope = ComposerScope(tools = listOf(scoped("proj:a", "project"), scoped("sys:off", "system", enabled = false)))
        assertEquals(listOf(ToolFacet("project", 1)), scope.activeToolFacets())
    }

    @Test
    fun `facets are empty and summary null while the catalog hasn't loaded`() {
        val scope = ComposerScope(tools = null)
        assertTrue(scope.activeToolFacets().isEmpty())
        assertNull(scope.toolsFacetSummary)
    }

    @Test
    fun `facet counts sum to activeToolCount`() {
        val scope = ComposerScope(
            tools = listOf(scoped("proj:a", "project"), scoped("sys:a", "system"), scoped("x", null), scoped("off", "system", enabled = false)),
        )
        assertEquals(scope.activeToolCount, scope.activeToolFacets().sumOf { it.count })
    }

    // --- toolsCompactSummary (scope-row last-resort truncation ladder) ---

    @Test
    fun `toolsCompactSummary is null while the tools catalog hasn't loaded`() {
        val scope = ComposerScope(tools = null)
        assertNull(scope.toolsCompactSummary)
    }

    @Test
    fun `toolsCompactSummary is the plain total even when facets are recognized`() {
        val scope = ComposerScope(
            tools = listOf(scoped("proj:a", "project"), scoped("proj:b", "project"), scoped("sys:a", "system"), scoped("plug:a", "plugin")),
        )
        assertEquals("4 tools", scope.toolsCompactSummary)
        // The full form stays faceted - compact is a SEPARATE, deliberately shorter fallback, never
        // a mutation of toolsFacetSummary.
        assertEquals("2 project · 1 system · 1 plugin", scope.toolsFacetSummary)
    }

    @Test
    fun `toolsCompactSummary tracks a narrowed selection via activeToolCount`() {
        val scope = ComposerScope(tools = listOf(toolA, toolB, toolC)).toggleTool(toolA.toolId)
        assertEquals("1 tools", scope.toolsCompactSummary)
        assertEquals(1, scope.activeToolCount)
    }
}
