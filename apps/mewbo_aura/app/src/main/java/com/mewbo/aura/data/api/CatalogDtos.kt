package com.mewbo.aura.data.api

import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.model.ToolSummary
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class ModelsResponseDto(
    val models: List<String> = emptyList(),
    val default: String = "",
    val capabilities: Map<String, ModelCapabilityDto> = emptyMap(),
)

@Serializable
data class ModelCapabilityDto(@SerialName("supports_vision") val supportsVision: Boolean = false)

@Serializable
data class ProjectDto(
    val name: String,
    val available: Boolean = true,
    val source: String = "config",
    @SerialName("project_id") val projectId: String? = null,
    @SerialName("is_worktree") val isWorktree: Boolean = false,
    val branch: String? = null,
) {
    fun toDomain() = ProjectSummary(
        name = name,
        available = available,
        source = source,
        projectId = projectId,
        isWorktree = isWorktree,
        branch = branch,
    )
}

@Serializable
data class ProjectsResponseDto(val projects: List<ProjectDto> = emptyList())

@Serializable
data class ToolDto(
    @SerialName("tool_id") val toolId: String,
    val name: String,
    val kind: String = "builtin",
    val enabled: Boolean = true,
    @SerialName("disabled_reason") val disabledReason: String? = null,
    val server: String? = null,
    // `global`/`project`/`plugin` — the backend has always sent this, but nothing
    // client-side read it until now. `scope == "plugin"` is what distinguishes a capability-gated
    // product tool (wiki_*, scg_*, agentic_search) from a plain core builtin (both are
    // `kind == "builtin"`) at the repository filter (SessionScopeRepository.tools()).
    val scope: String? = null,
) {
    fun toDomain() = ToolSummary(
        toolId = toolId,
        name = name,
        kind = kind,
        enabled = enabled,
        server = server,
        disabledReason = disabledReason,
        scope = scope,
    )
}

@Serializable
data class ToolsResponseDto(val tools: List<ToolDto> = emptyList())
