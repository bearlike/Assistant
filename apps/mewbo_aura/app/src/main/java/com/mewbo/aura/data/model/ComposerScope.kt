package com.mewbo.aura.data.model

import androidx.compose.runtime.Immutable

/**
 * Project + MCP-tool scoping for the NEXT fresh-turn send/session-create (composer "+" sheet,
 * Gitea #177 W2). Catalogs ([projects]/[tools]) are `null` until
 * [com.mewbo.aura.ui.chat.ChatViewModel.refreshComposerScope] resolves them. Selections are frozen
 * in the UI once a session exists, but the backend independently re-resolves `project`/`mcp_tools`
 * from EVERY `/query` call's own request body (nothing is sticky across turns server-side -
 * data/CLAUDE.md), so [selectedProjectKey]/[activeToolIds] must keep flowing into every send, not
 * just the first.
 */
@Immutable
data class ComposerScope(
    val projects: List<ProjectSummary>? = null,
    val tools: List<ToolSummary>? = null,
    val selectedProjectKey: String? = null,
    /** `null` = untouched (every tool at its own catalog default); non-null = the user's explicit set. */
    val activeToolIds: Set<String>? = null,
) {
    val projectDisplayName: String
        get() = when {
            // Genuinely no project chosen -> a temp-dir cwd (data/CLAUDE.md); "Temporary" is correct.
            selectedProjectKey == null -> "Temporary"
            // A project IS selected but the catalog can't resolve it to a name yet: [projects] is still
            // null (not fetched), or a revisited session hydrated [selectedProjectKey] before
            // refreshComposerScope loaded the catalog (commit 3add535). Never claim "Temporary" here -
            // that was the user-reported revisit-race bug (Gitea #185 P6). Degrade to the raw key until
            // the catalog resolves it, mirroring the sheet's "rows degrade to their placeholder label"
            // posture (ComposerOptionsSheet KDoc) rather than showing a factually wrong label.
            else -> projects.orEmpty().firstOrNull { it.contextKey == selectedProjectKey }?.name ?: selectedProjectKey
        }

    /** Tool count for the pre-session scope indicator (`ChatSurface`, Gitea #178 W1-B) - `null`
     * while [tools] hasn't loaded yet (the indicator shows just the project name, no count). */
    val activeToolCount: Int?
        get() = tools?.let { resolvedActiveToolIds.size }

    private val defaultActiveToolIds: Set<String>
        get() = tools.orEmpty().filter { it.enabled }.mapTo(mutableSetOf()) { it.toolId }

    private val resolvedActiveToolIds: Set<String>
        get() = activeToolIds ?: defaultActiveToolIds

    /** `true` once the resolved set diverges from every tool's own catalog default - the signal
     * that gates [mcpToolsForContext] (task brief: untouched -> omit the field entirely). */
    val toolsNarrowed: Boolean
        get() = activeToolIds != null && activeToolIds != defaultActiveToolIds

    val toolsSummaryLabel: String
        get() = if (toolsNarrowed) "${resolvedActiveToolIds.size} of ${tools?.size ?: 0} on" else "All tools"

    fun isToolActive(toolId: String): Boolean = toolId in resolvedActiveToolIds

    /** Flips one tool's membership relative to the CURRENT resolved set - materializes the
     * previously-implicit default set into [activeToolIds] the first time a tool is touched (the
     * moment [toolsNarrowed] can go true), and can return to `false` again on an exact-default
     * toggle sequence. */
    fun toggleTool(toolId: String): ComposerScope {
        val next = resolvedActiveToolIds.let { ids -> if (toolId in ids) ids - toolId else ids + toolId }
        return copy(activeToolIds = next)
    }

    /** Bulk variant of [toggleTool] for a whole server group (toggle-sheet server-row "all on"/"all
     * off"): materializes the resolved set the same way, then adds or removes every id in [toolIds]
     * in one step rather than one [toggleTool] call per tool. */
    fun setServerTools(toolIds: Collection<String>, active: Boolean): ComposerScope {
        val next = if (active) resolvedActiveToolIds + toolIds else resolvedActiveToolIds - toolIds.toSet()
        return copy(activeToolIds = next)
    }

    /** Active-vs-total counts among tools sharing a toggle-sheet [ToolSummary.groupKey] - backs the
     * per-server "N of M on" caption. */
    fun activeCountFor(groupKey: String): Pair<Int, Int> {
        val group = tools.orEmpty().filter { it.groupKey == groupKey }
        return group.count { it.toolId in resolvedActiveToolIds } to group.size
    }

    /** `context.mcp_tools` value - omitted entirely (`null`) unless the user narrowed (task brief:
     * untouched means the backend binds every tool, builtins + MCP). */
    fun mcpToolsForContext(): List<String>? = if (toolsNarrowed) resolvedActiveToolIds.toList() else null
}
