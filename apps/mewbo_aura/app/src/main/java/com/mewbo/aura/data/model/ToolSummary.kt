package com.mewbo.aura.data.model

import androidx.compose.runtime.Immutable

/** `GET api/tools` entry: MCP tools plus capability-gated product tools (Gitea #182 P3) -
 * [com.mewbo.aura.data.repo.SessionScopeRepository.tools] excludes only plain core builtins
 * (shell, edit, ...), which have no per-run allowlist knob, before this type is ever constructed. */
@Immutable
data class ToolSummary(
    val toolId: String,
    val name: String,
    val kind: String,
    val enabled: Boolean,
    val server: String? = null,
    val disabledReason: String? = null,
    /** Provenance tag from `GET api/tools` (`builtin`/`project`/`system`/`plugin`, Gitea #185 P4) -
     * surfaced as a per-group label in the Tools pane. `null` when the backend omitted it. */
    val scope: String? = null,
) {
    /** Toggle-sheet grouping key (task brief: "group by server ?: name"). */
    val groupKey: String get() = server ?: name
}
