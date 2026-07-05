package com.mewbo.aura.data.model

import androidx.compose.runtime.Immutable
import kotlinx.serialization.json.JsonElement

/**
 * Renderable transcript item produced by [TranscriptReducer]. Every variant carries a stable
 * [key] for `LazyColumn` item identity - stable across re-reduction of the same event log so
 * streaming updates recompose in place instead of re-animating a new item.
 *
 * `@Immutable` is a Compose *stability* contract, not a UI dependency - `androidx.compose.runtime`
 * has no Android/UI-toolkit code in it (unlike `androidx.compose.ui`/`material3`, which stay
 * forbidden here per data/CLAUDE.md). It's load-bearing: [TranscriptReducer] always hands back a
 * fresh instance per changed item (never mutates one in place - see `upsert`), but the compiler
 * can't infer that on its own because of the `List<ChatTodoItem>` field on [ChatItem.TodoList];
 * without this annotation every `ChatItem` is inferred unstable, which defeats Compose's
 * equals-based skip for every list row, not just the changed one (Task E review, fix round 1).
 */
@Immutable
sealed interface ChatItem {
    val key: String

    /**
     * [pending] is true only for the client's own optimistic echo of a send that steered into an
     * already-active run (202 enqueue, spec §6.2 C4/§6.12) - the row renders at 70% opacity until
     * the server's real `user`/`user_steer` echo arrives and [TranscriptReducer] flips it back to
     * `false` in place (same [key], no re-animation).
     */
    data class UserBubble(
        val text: String,
        val ts: String,
        val pending: Boolean = false,
        /** Metadata-only files riding this send (Gitea attachments wire-up, 2026-07-03) - rendered
         * as a right-aligned tile row above the bubble by [com.mewbo.aura.ui.chat.UserBubbleRow].
         * Populated both from the optimistic local echo ([com.mewbo.aura.ui.chat.ChatViewModel.send]'s
         * staged attachments) and from the backend's persisted `user`/`user_steer` event payload on
         * replay - never a separate fetch. */
        val attachments: List<AttachmentSummary> = emptyList(),
        override val key: String,
    ) : ChatItem

    data class AssistantMessage(
        val text: String,
        val isStreaming: Boolean,
        val ts: String,
        override val key: String,
    ) : ChatItem

    /**
     * A reference-style fold group of one turn's `tool_result` events (Gitea #177 W1-B, replaces the
     * old one-pill-per-event `ActivityChip`). [key] is the first call's key, stable across later
     * appends ([TranscriptReducer] upserts the SAME key as more `tool_result`s land in the group).
     */
    data class ToolCallGroup(
        val calls: List<ToolCall>,
        override val key: String,
    ) : ChatItem

    data class ErrorCard(
        val message: String,
        val ts: String,
        override val key: String,
    ) : ChatItem

    /** Singleton card - `key` is a constant so each new `todos` event replaces it in place. */
    data class TodoList(
        val items: List<ChatTodoItem>,
        val ts: String,
        override val key: String = "todos",
    ) : ChatItem

    /**
     * A sub-agent's spawn/lifecycle, rendered as its own chip family (Rev D §D-5) - NOT an
     * [ActivityChip] (`tool_result` is a different backend signal than `sub_agent`). One item per
     * `agentId`, upserted in place as later `sub_agent` events for the same id arrive (the reducer
     * updates [status]/[stepsCompleted]/[terminal]/[success] on the SAME [key], never appends a
     * second row).
     */
    data class AgentChip(
        val agentId: String,
        val agentType: String?,
        /** The backend's `status` field, falling back to its `action` when no status has arrived
         * yet (spec D-5: "label = {agentType-or-shortId} · {status-or-action}"). */
        val status: String?,
        val stepsCompleted: Int?,
        val terminal: Boolean,
        /** Only meaningful once [terminal] - `true` for `completed`, `false` for
         * `failed`/`cancelled`/`rejected`, `null` while still in progress. */
        val success: Boolean?,
        val ts: String,
        override val key: String,
    ) : ChatItem
}

@Immutable
data class ChatTodoItem(val label: String, val status: String)

/**
 * Metadata-only attachment descriptor for a [ChatItem.UserBubble] - filename + type, NEVER a real
 * thumbnail/image (no Coil/AsyncImage anywhere in this app; the transcript renders indicators, not
 * pixels). Field names deliberately mirror [com.mewbo.aura.data.api.AttachmentRecordDto] (minus the
 * upload-plumbing-only `id`/`storedName`/`uploadedAt`/`parsed` fields this display-only model has no
 * use for).
 */
@Immutable
data class AttachmentSummary(
    val filename: String,
    val mimeType: String,
    val sizeBytes: Long? = null,
)

/** One `tool_result` event within a [ChatItem.ToolCallGroup]. [key] is per-call so the row's own
 * in-place detail expansion (input/result) survives group re-composition when a later call is
 * appended to the same group. */
@Immutable
data class ToolCall(
    val toolId: String,
    val operation: String,
    val inputJson: JsonElement?,
    val summary: String?,
    val success: Boolean,
    val detail: String?,
    val error: String?,
    val model: String?,
    val ts: String,
    val key: String,
)
