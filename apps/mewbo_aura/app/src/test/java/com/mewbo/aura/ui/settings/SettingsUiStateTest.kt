package com.mewbo.aura.ui.settings

import com.mewbo.aura.data.model.ComposerScope
import com.mewbo.aura.data.model.ModelCapabilities
import com.mewbo.aura.data.model.ModelCatalog
import com.mewbo.aura.data.model.ProjectSummary
import org.junit.Assert.assertEquals
import org.junit.Test

/** Coverage for [resolveProjectDisplayName] (default-project setting) and
 * [resolveModelDisplayName] (per-surface model defaults). */
class SettingsUiStateTest {

    private val configProject = ProjectSummary(name = "Assistant", source = "config")
    private val managedProject = ProjectSummary(name = "scratch-worktree", source = "managed", projectId = "abc123")

    private val catalog = ModelCatalog(
        rawModels = listOf("claude-sonnet-5", "claude-opus-4-8"),
        default = "openai/claude-sonnet-5",
        capabilities = mapOf("claude-sonnet-5" to ModelCapabilities(supportsVision = true)),
    )

    @Test
    fun `a blank model resolves to Default regardless of the catalog`() {
        assertEquals("Default", resolveModelDisplayName(modelId = "", models = null))
        assertEquals("Default", resolveModelDisplayName(modelId = "", models = catalog))
    }

    @Test
    fun `a known model id resolves to the catalog's pretty display name`() {
        assertEquals("Claude Sonnet 5", resolveModelDisplayName(modelId = "claude-sonnet-5", models = catalog))
        assertEquals("Claude Opus 4.8", resolveModelDisplayName(modelId = "claude-opus-4-8", models = catalog))
    }

    @Test
    fun `a non-blank model degrades to the normalized raw id when the catalog hasn't loaded`() {
        assertEquals("claude-sonnet-5", resolveModelDisplayName(modelId = "openai/claude-sonnet-5", models = null))
    }

    @Test
    fun `an empty key resolves to Temporary regardless of the catalog`() {
        assertEquals("Temporary", resolveProjectDisplayName(selectedProject = "", projects = null))
        assertEquals("Temporary", resolveProjectDisplayName(selectedProject = "", projects = listOf(configProject)))
    }

    @Test
    fun `a config project's bare-name key resolves to its display name`() {
        val result = resolveProjectDisplayName(selectedProject = "Assistant", projects = listOf(configProject, managedProject))
        assertEquals("Assistant", result)
    }

    @Test
    fun `a managed project's contextKey resolves to its display name`() {
        val result = resolveProjectDisplayName(selectedProject = "managed:abc123", projects = listOf(configProject, managedProject))
        assertEquals("scratch-worktree", result)
    }

    @Test
    fun `the auto sentinel resolves to Auto, never the raw stored key`() {
        // It resolves against no catalog, so the lookup would print the raw "auto" — and the loop
        // closes: the picker offers a row labelled "Auto", persists this key, and the settings row
        // beneath it then disagrees with the picker that set it. Asserted with the catalog BOTH
        // absent and present, because the sentinel must never depend on the catalog at all.
        assertEquals("Auto", resolveProjectDisplayName(ComposerScope.AUTO_PROJECT_KEY, null))
        assertEquals(
            "Auto",
            resolveProjectDisplayName(ComposerScope.AUTO_PROJECT_KEY, listOf(configProject, managedProject)),
        )
    }

    @Test
    fun `a non-empty key degrades to the raw stored value when the catalog hasn't loaded`() {
        val result = resolveProjectDisplayName(selectedProject = "managed:abc123", projects = null)
        assertEquals("managed:abc123", result)
    }

    @Test
    fun `a non-empty key degrades to the raw stored value when the catalog has no match`() {
        val result = resolveProjectDisplayName(selectedProject = "managed:stale-id", projects = listOf(configProject, managedProject))
        assertEquals("managed:stale-id", result)
    }
}
