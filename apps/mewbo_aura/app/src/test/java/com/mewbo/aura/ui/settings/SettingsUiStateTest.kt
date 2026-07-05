package com.mewbo.aura.ui.settings

import com.mewbo.aura.data.model.ProjectSummary
import org.junit.Assert.assertEquals
import org.junit.Test

/** Coverage for [resolveProjectDisplayName] (Gitea #178 W1-A default-project setting). */
class SettingsUiStateTest {

    private val configProject = ProjectSummary(name = "Assistant", source = "config")
    private val managedProject = ProjectSummary(name = "scratch-worktree", source = "managed", projectId = "abc123")

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
