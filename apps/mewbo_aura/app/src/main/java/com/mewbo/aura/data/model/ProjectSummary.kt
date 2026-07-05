package com.mewbo.aura.data.model

import androidx.compose.runtime.Immutable

/** `GET api/projects` entry (task brief W2) - config-defined or server-managed. */
@Immutable
data class ProjectSummary(
    val name: String,
    val available: Boolean = true,
    val source: String = "config",
    val projectId: String? = null,
    val isWorktree: Boolean = false,
    val branch: String? = null,
) {
    /** `context.project` value: bare name for config projects, `managed:<id>` for managed ones -
     * mirrors the web console's `projectKey()` (`ConfigMenu.tsx`). */
    val contextKey: String
        get() = if (source == "managed" && !projectId.isNullOrBlank()) "managed:$projectId" else name
}
