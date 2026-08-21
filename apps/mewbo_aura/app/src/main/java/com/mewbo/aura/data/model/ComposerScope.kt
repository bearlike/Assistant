package com.mewbo.aura.data.model

import androidx.compose.runtime.Immutable

/**
 * Project + MCP-tool scoping for the NEXT fresh-turn send/session-create (composer "+" sheet).
 * Catalogs ([projects]/[tools]) are `null` until
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
    /** `true` while the session is in auto-select mode ([AUTO_PROJECT_KEY]) - no project is fixed
     * and the model picks one with `switch_project`. Distinct from `selectedProjectKey == null`,
     * which is the throwaway temp-dir cwd nobody will move off. */
    val isAutoProject: Boolean
        get() = selectedProjectKey == AUTO_PROJECT_KEY

    val projectDisplayName: String
        get() = when {
            // Genuinely no project chosen -> a temp-dir cwd (data/CLAUDE.md); "Temporary" is correct.
            selectedProjectKey == null -> "Temporary"
            // The sentinel is not a project and will never resolve against [projects] - label it
            // before the catalog lookup below, which would otherwise degrade it to the raw "auto".
            selectedProjectKey == AUTO_PROJECT_KEY -> "Auto"
            // A project IS selected but the catalog can't resolve it to a name yet: [projects] is still
            // null (not fetched), or a revisited session hydrated [selectedProjectKey] before
            // refreshComposerScope loaded the catalog. Never claim "Temporary" here -
            // that misrepresents a real, still-resolving project as no project at all. Degrade to
            // the raw key until
            // the catalog resolves it, mirroring the sheet's "rows degrade to their placeholder label"
            // posture (ComposerOptionsSheet KDoc) rather than showing a factually wrong label.
            else -> projects.orEmpty().firstOrNull { it.contextKey == selectedProjectKey }?.name ?: selectedProjectKey
        }

    /**
     * The open-session chrome's "where am I working" readout (`ChatTopBar`) - [projectDisplayName]
     * for a scoped session, `null` when the session has no project at all, so the bar stays clean
     * rather than carrying a permanent "Temporary" line for the throwaway-cwd case.
     *
     * It reads the SAME [selectedProjectKey] the next `/query` resends, which is what makes it
     * follow a mid-run project switch: `ChatViewModel` adopts every persisted `context` event's
     * project as the event arrives, and a `switch_project` writes one.
     */
    val activeProjectLabel: String?
        get() = selectedProjectKey?.let { projectDisplayName }

    /**
     * The project key a tool-catalog fetch ([com.mewbo.aura.data.repo.SessionScopeRepository.tools])
     * is scoped to: the selection itself, EXCEPT in auto mode, where it is `null` (unscoped).
     * [AUTO_PROJECT_KEY] names no directory - the resolver refuses it outright rather than treating
     * it as "no project" - so scoping a catalog to it would ask for tools from a workspace that
     * does not exist yet, where the honest answer is the unscoped superset the model picks from.
     */
    val toolScopeKey: String?
        get() = toolScopeKeyOf(selectedProjectKey)

    /** Tool count for the pre-session scope indicator (`ChatSurface`) - `null`
     * while [tools] hasn't loaded yet (the indicator shows just the project name, no count). */
    val activeToolCount: Int?
        get() = tools?.let { resolvedActiveToolIds.size }

    /**
     * Provenance-faceted breakdown of the ACTIVE (resolved) tools for the scope row (user directive:
     * show grouped counts like "2 project · 5 system · 3 plugin" instead of one total).
     * Facets are ordered by [FACET_ORDER] (most user-relevant first), zero facets dropped, and any
     * tool whose [ToolSummary.scope] is null or unrecognized is collected into [FACET_OTHER]. Empty
     * while the catalog hasn't loaded. Counts sum to [activeToolCount] by construction.
     */
    fun activeToolFacets(): List<ToolFacet> {
        val loaded = tools ?: return emptyList()
        val active = loaded.filter { it.toolId in resolvedActiveToolIds }
        val counts = active.groupingBy { it.scope?.takeIf { s -> s in FACET_ORDER } ?: FACET_OTHER }.eachCount()
        return (FACET_ORDER + FACET_OTHER).mapNotNull { key ->
            counts[key]?.takeIf { it > 0 }?.let { ToolFacet(key, it) }
        }
    }

    /** Scope-row tools summary: the faceted "2 project · 5 system · 3 plugin" string once any active
     * tool carries a recognized scope; the plain "N tools" total when none do (older backend that
     * omits [ToolSummary.scope] — never a lone, meaningless "N other"); `null` while the catalog
     * hasn't loaded (the row then shows the project name alone, unchanged pre-load behavior). Prefer
     * this form whenever it fits the row's available width; [toolsCompactSummary] is the fallback. */
    val toolsFacetSummary: String?
        get() {
            val count = activeToolCount ?: return null
            val facets = activeToolFacets()
            if (facets.none { it.scope != FACET_OTHER }) return "$count tools"
            return facets.joinToString(" · ") { "${it.count} ${it.scope}" }
        }

    /** Compact one-line fallback for [toolsFacetSummary] — plain "N tools", no facet breakdown. The
     * scope row (`ChatSurface`'s `ComposerScopeIndicator`) collapses to this form as the SECOND rung
     * of its last-resort truncation ladder (truncation is a last resort, so the faceted
     * breakdown degrades to this compact total under genuine width pressure BEFORE any text is
     * allowed to ellipsize). `null` under the same load condition as [toolsFacetSummary]. */
    val toolsCompactSummary: String?
        get() = activeToolCount?.let { "$it tools" }

    private val defaultActiveToolIds: Set<String>
        get() = tools.orEmpty().filter { it.enabled }.mapTo(mutableSetOf()) { it.toolId }

    private val resolvedActiveToolIds: Set<String>
        get() = activeToolIds ?: defaultActiveToolIds

    /** `true` once the user's explicit set diverges from every tool's own catalog default - the
     * signal that gates [mcpToolsForContext] (task brief: untouched -> omit the field entirely).
     *
     * **The "equals the default, so omit it again" arm is only sound once [tools] has LOADED**, and
     * that guard is load-bearing rather than defensive. [defaultActiveToolIds] derives from the
     * catalog, so before it arrives the default reads as the EMPTY set - which made an explicit
     * "every tool off" ([activeToolIds] empty) compare equal to it and report not-narrowed, sending
     * no ceiling at all. That is the same fail-open [mcpToolsForContext]'s wire seam closes, one
     * layer up: a selection hydrated from the session's own persisted `mcp_tools` is re-declared
     * verbatim while the catalog is still in flight, never silently widened back to everything. */
    val toolsNarrowed: Boolean
        get() = activeToolIds != null && (tools == null || activeToolIds != defaultActiveToolIds)

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

    /**
     * `context.mcp_tools` value, THREE-STATE - `null` omits the key (untouched: the backend binds
     * every tool, builtins + MCP), an EMPTY list declares a ceiling of zero, a non-empty list
     * declares exactly those. Empty is a real answer here, never a stand-in for `null`: the seam
     * that writes it ([com.mewbo.aura.data.repo.buildSessionContext]) tests `!= null` rather than
     * truthiness, so "I turned every tool off" survives to the wire as `[]` instead of arriving as
     * silence and re-binding the whole registry.
     */
    fun mcpToolsForContext(): List<String>? = if (toolsNarrowed) resolvedActiveToolIds.toList() else null

    companion object {
        /**
         * The reserved `context.project` value meaning "no fixed project — Mewbo picks one, and may
         * move the session between projects mid-run" (core's `project_catalog.AUTO_PROJECT`). A
         * sentinel sibling to the `managed:<id>` grammar ([ProjectSummary.contextKey]), NOT a
         * project: it never appears in the catalog and resolves to no directory until a
         * `switch_project` names one, which is why the resolver REFUSES it instead of quietly
         * handing back a temp dir.
         *
         * Contrast `selectedProjectKey == null`, which sends no `project` field at all — a
         * throwaway temp-dir cwd the session stays in. Spelled here exactly once: a picker row, a
         * display label and a wire value that each carried their own copy of a sentinel is how
         * `app:<app_id>` came to be stamped with no reader for it.
         */
        const val AUTO_PROJECT_KEY = "auto"

        /** [toolScopeKey] for a key held on its own rather than in a live scope - what
         * `ChatViewModel.selectProject` needs, since it must scope the re-fetch to the key the USER
         * just tapped, not to whatever a concurrent second tap has since put into state. */
        fun toolScopeKeyOf(projectKey: String?): String? = projectKey?.takeUnless { it == AUTO_PROJECT_KEY }

        /** Display/section order for tool [ToolSummary.scope] provenance — most user-relevant first
         * (a project's own tools before shared system/plugin/core ones). Drives both the scope-row
         * facet order ([activeToolFacets]) and the tool picker's scope-section grouping. */
        val FACET_ORDER = listOf("project", "device", "system", "plugin", "builtin")

        /** The scope tag carried by client-declared `device_*` tools. They never come
         * from `GET api/tools` — the client DECLARES them per query — so this is
         * stamped locally rather than read off the wire. Second in [FACET_ORDER]
         * because a tool that acts on the user's own phone is the one they most need
         * to see. */
        const val FACET_DEVICE = "device"

        /** Bucket for a null/unrecognized [ToolSummary.scope] — the older-backend fallback. */
        const val FACET_OTHER = "other"
    }
}

/** One provenance facet of the active tool set — a [scope] tag ("project"/"system"/… or
 * [ComposerScope.FACET_OTHER]) and how many active tools carry it. Ordered, non-zero-only; produced
 * by [ComposerScope.activeToolFacets]. */
data class ToolFacet(val scope: String, val count: Int)
