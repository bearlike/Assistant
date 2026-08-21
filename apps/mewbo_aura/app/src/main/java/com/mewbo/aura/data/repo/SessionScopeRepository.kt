package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.device.DeviceControlSession
import com.mewbo.aura.data.device.DeviceToolCatalog
import com.mewbo.aura.data.model.ComposerScope
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.model.ToolSummary
import javax.inject.Inject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.Flow

/** The composer options sheet's two catalogs (`GET api/projects`, `GET api/tools`) - plain suspend
 * fetches, no caching (task brief: the sheet re-fetches on every open, same as [ModelRepository]'s
 * `null`-on-failure degrade but without the cache). */
class SessionScopeRepository @Inject constructor(
    private val api: AuraApi,
    private val deviceToolCatalog: DeviceToolCatalog,
    deviceControlSession: DeviceControlSession,
) {
    /**
     * Emits when a [tools] re-fetch would return a DIFFERENT device-tool list.
     *
     * [tools] is always fresh when called; the staleness is on the surface that
     * called it once and kept the answer. Measured: after authorising Shizuku,
     * Settings read "Ready" while the composer still read the pre-authorisation
     * device-tool count, and only a force-stop corrected it — the toggles and
     * the picker were describing the same session and disagreeing about it.
     *
     * A plain change signal rather than a tool `Flow`: the picker's fetch is
     * project-scoped and degrade-to-null, and re-deriving that here would
     * duplicate [tools] rather than reuse it. A collector re-runs the fetch it
     * already has.
     */
    val deviceToolsChanged: Flow<Unit> = deviceControlSession.changes

    // both fetches run inside ChatViewModel's own async{} pairs
    // (refreshComposerScope/bind's prefetch/selectProject) - a cancelled coroutine there must
    // propagate, not resolve to null and let the caller act on a "failure" that was really a
    // cancellation. rethrowCancellation before falling back to the degrade-to-null path.
    suspend fun projects(): List<ProjectSummary>? =
        runCatching { api.getProjects().projects.map { it.toDomain() } }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()

    /** MCP tools, plus capability-gated product tools (wiki_*, scg_*, agentic_search -
     * `server`-grouped entries at `scope == "plugin"`). Plain core builtins (shell, edit, ...)
     * stay excluded - they have no per-run allowlist knob and would flood the picker with entries
     * nobody can usefully toggle. The backend's own `classify_tool_scope` always resolves a
     * non-MCP core builtin to `scope == "builtin"`, so `scope == "plugin"` cleanly picks out ONLY
     * the new product rows without a second `kind` check (a plugin-sourced MCP server also happens
     * to read `scope == "plugin"`, but it's already included via `kind == "mcp"` - no double-count
     * risk, this is a plain OR). */
    suspend fun tools(project: String? = null): List<ToolSummary>? =
        runCatching {
            api.getTools(project).tools
                .filter { it.kind == "mcp" || it.scope == "plugin" }
                .map { it.toDomain() } + deviceTools()
        }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()

    /**
     * The `device_*` tools this session would actually advertise, as picker rows.
     *
     * **They are stamped here rather than fetched, because the server never sees
     * them as a catalog.** A device tool is DECLARED by the client on each query,
     * so it appears in no `GET api/tools` response and consequently appeared in no
     * group of the picker — the one tool family that acts on the user's own phone,
     * including a shell, was the one family absent from the surface built for
     * seeing and controlling tool exposure.
     *
     * The list is the ADVERTISED one, so a tool the user switched off in Settings,
     * or one whose permission is missing, is absent here too — the picker shows
     * what this session can really do, not what the build ships.
     */
    private suspend fun deviceTools(): List<ToolSummary> =
        deviceToolCatalog.availableTools().map {
            ToolSummary(
                toolId = it.toolId,
                name = it.toolId.removePrefix("device_").replace('_', ' '),
                kind = "device",
                enabled = true,
                server = "This device",
                scope = ComposerScope.FACET_DEVICE,
            )
        }
}
