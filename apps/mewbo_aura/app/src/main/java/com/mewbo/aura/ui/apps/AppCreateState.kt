package com.mewbo.aura.ui.apps

import com.mewbo.aura.data.model.ProjectSummary

/** Own vs shared workspace choice (design spec §2.3: "Own-workspace default, shared-workspace
 * option"). [AppWorkspaceChoice.Shared] carries the FULL [ProjectSummary] (not just its key) so the
 * creation screen can render the picked project's display name without a second lookup. */
sealed interface AppWorkspaceChoice {
    data object Own : AppWorkspaceChoice
    data class Shared(val project: ProjectSummary) : AppWorkspaceChoice
}

/**
 * Creation screen phase — a lighter "building" indicator than the full chat transcript (task brief:
 * "reuse the existing run-streaming UI if trivially reusable, else show building state that
 * refreshes"). [AppCreateViewModel] follows [com.mewbo.aura.data.repo.RunRepository.live] directly
 * (the SAME seam chat/the overlay/the notification watcher already share) rather than forking a
 * second SSE path, but renders only a narration line + terminal outcome, not the full
 * [com.mewbo.aura.ui.chat.ChatTranscript] — deliberately lighter-weight than the full chat surface
 * for a screen whose only job is "wait, then hand off to app detail."
 */
sealed interface AppCreatePhase {
    /** The intent/workspace form is still editable. */
    data object Composing : AppCreatePhase
    data object Submitting : AppCreatePhase

    /** [narration] is the most recent sub-agent detail or open todo label seen on the builder
     * session's live stream — `null` until the first one arrives. */
    data class Building(val narration: String?) : AppCreatePhase
    data class Ready(val appId: String) : AppCreatePhase
    data class Failed(val message: String) : AppCreatePhase
}
