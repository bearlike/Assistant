package com.mewbo.aura.data.model

import java.time.Duration
import kotlin.math.abs

/**
 * Pure, idempotent fold from the raw session event log into renderable [ChatItem]s. The SAME
 * event list always produces the SAME items - this is what makes SSE reconnect (full backlog
 * replay, api-contract.md section 3) and `/events?after=` backfill overlap safe: callers just
 * re-run [reduce] over the whole accumulated event list and duplicates collapse away.
 *
 * Contract invariants encoded here (see data/CLAUDE.md):
 *  - Content-key dedupe (`SessionEvent.toString()`, which already includes type+ts+payload)
 *    drops exact-repeat frames from backlog replay/backfill overlap.
 *  - `agent_message_delta` accumulates into one in-flight assistant item; `agent_message`
 *    REPLACES that buffer (drift reconciliation); `assistant` finalizes it.
 *  - Only root-agent narration (`depth == 0`) reaches the chat lane; sub-agent narration is
 *    dropped, matching "Transcript renders root-agent text only".
 *  - Optimistic local echo vs. the server's `user`/`user_steer` event is deduped by text +
 *    ts-window identity, not exact equality (their `ts` values differ).
 *  - `todos` is authoritative and replaces the whole checklist card in place on every event.
 *  - `completion`/`stream_end` close any open streaming state.
 *  - **Activity-precedes-narration**: within a turn, activity items (tool groups, agent chips,
 *    todo list) always precede the turn's assistant text item; a turn's text item is always its
 *    last item (error cards excepted - they always append at the very end). Real sessions
 *    interleave `tool_result`s with root narration deltas in either order within one turn
 *    (verified: 9/122 aura-android turns), so a tool group/chip/todo-list that first appears
 *    AFTER the turn's assistant-text item has already opened is inserted ahead of it instead of
 *    appended - see [appendOrInsertBeforeOpenAssistant].
 */
object TranscriptReducer {

    /** Carries fold progress across incremental SSE frames without re-reducing full history. */
    @ConsistentCopyVisibility
    data class State internal constructor(
        internal val items: List<ChatItem> = emptyList(),
        internal val indexByKey: Map<String, Int> = emptyMap(),
        internal val seenContentKeys: Set<String> = emptySet(),
        internal val openAssistantKey: String? = null,
        internal val assistantBuffer: String = "",
        /** Key of the [ChatItem.ToolCallGroup] still accepting consecutive `tool_result`s (Gitea
         * #177 W1-B) - cleared ONLY when a fresh user/user_steer turn begins ([foldUserText]), so
         * the NEXT `tool_result` starts a new group instead of appending to a closed one. Root
         * narration opening/continuing the turn's assistant-text item no longer clears this - a
         * turn's tool calls and its narration interleave freely in real sessions, and they all
         * belong in ONE group per turn (see the file header's activity-precedes-narration note). */
        internal val openToolGroupKey: String? = null,
    ) {
        val chatItems: List<ChatItem> get() = items
    }

    fun reduce(events: List<SessionEvent>): List<ChatItem> =
        events.fold(State(), ::fold).chatItems

    /**
     * [pending] only ever applies to the CALLER's own optimistic local echo of a [SessionEvent.User]
     * it is about to send - every real wire event (history replay, live SSE) folds through the
     * default `false` and never sets it. [foldUserText] clears a bubble's `pending` the moment the
     * server's real echo dedupe-matches it, regardless of what this call's [pending] argument was.
     */
    fun fold(state: State, event: SessionEvent, pending: Boolean = false): State {
        val contentKey = event.toString()
        if (contentKey in state.seenContentKeys) return state
        val deduped = state.copy(seenContentKeys = state.seenContentKeys + contentKey)

        return when (event) {
            is SessionEvent.User -> foldUserText(deduped, event.payload, event.ts, pending)
            is SessionEvent.UserSteer -> foldUserText(deduped, event.payload, event.ts, pending)
            is SessionEvent.AgentMessageDelta ->
                if (event.payload.depth != 0) deduped else appendDelta(deduped, event.payload.text, event.ts)
            is SessionEvent.AgentMessage ->
                if (event.payload.depth != 0) deduped else replaceBuffer(deduped, event.payload.text, event.ts)
            is SessionEvent.Assistant -> finalizeAssistant(deduped, event.payload.text, event.ts)
            is SessionEvent.ToolResult -> foldToolResult(deduped, event)
            is SessionEvent.Completion -> foldCompletion(deduped, event)
            is SessionEvent.Todos -> foldTodos(deduped, event)
            is SessionEvent.SubAgent -> foldSubAgent(deduped, event)
            is SessionEvent.StreamEnd -> closeStreaming(deduped)
            is SessionEvent.LlmCallEnd,
            is SessionEvent.Permission,
            is SessionEvent.DeviceToolCall,
            is SessionEvent.StreamError, // intercepted by ChatViewModel before reaching fold(); never rendered
            is SessionEvent.Unknown,
            -> deduped
        }
    }

    private fun upsert(state: State, item: ChatItem): State {
        val index = state.indexByKey[item.key]
        return if (index != null) {
            state.copy(items = state.items.toMutableList().apply { this[index] = item })
        } else {
            state.copy(
                items = state.items + item,
                indexByKey = state.indexByKey + (item.key to state.items.size),
            )
        }
    }

    /**
     * Optimistic-echo dedupe: same text within [USER_ECHO_WINDOW_SECONDS] of an existing bubble is
     * a no-op for the bubble itself, EXCEPT it clears a lingering [ChatItem.UserBubble.pending] -
     * the second sighting of a turn (almost always the server's real echo) is exactly the signal
     * that a queued send was delivered (spec §6.12 "queued send... 100% on its `user` event").
     */
    private fun foldUserText(state: State, payload: TextPayload, ts: String, pending: Boolean): State {
        val text = payload.text
        val closed = closeToolGroup(state)
        val existing = closed.items.firstOrNull { item ->
            item is ChatItem.UserBubble && item.text == text && withinEchoWindow(item.ts, ts)
        } as? ChatItem.UserBubble
        if (existing != null) {
            return if (existing.pending) upsert(closed, existing.copy(pending = false)) else closed
        }
        val attachments = payload.attachments?.map {
            AttachmentSummary(filename = it.filename, mimeType = it.contentType, sizeBytes = it.sizeBytes)
        } ?: emptyList()
        return upsert(
            closed,
            ChatItem.UserBubble(text = text, ts = ts, pending = pending, attachments = attachments, key = "user:$ts:${text.hashCode()}"),
        )
    }

    private fun openAssistantIfNeeded(state: State, ts: String): State {
        if (state.openAssistantKey != null) return state
        val key = "assistant:$ts"
        val opened = upsert(state, ChatItem.AssistantMessage(text = "", isStreaming = true, ts = ts, key = key))
        return opened.copy(openAssistantKey = key, assistantBuffer = "")
    }

    /** Turn boundary for [ChatItem.ToolCallGroup] folding (see [foldToolResult]): a fresh user
     * turn closes whatever group is currently accepting appends. Opening/continuing the turn's
     * own assistant-text item no longer closes it - see [State.openToolGroupKey]. */
    private fun closeToolGroup(state: State): State = state.copy(openToolGroupKey = null)

    /**
     * Preserves the activity-precedes-narration invariant (file header) for a CREATE
     * (first-arrival) fold of a [ChatItem.ToolCallGroup]/[ChatItem.AgentChip]/[ChatItem.TodoList]:
     * when the current turn's assistant-text item already exists ([State.openAssistantKey]
     * non-null), the new activity item is INSERTED at that item's current index - pushing the
     * assistant item down - instead of appended after it. Successive inserts each land at the
     * assistant item's (updated) current index, so arrival order among activity items is
     * preserved. [indexByKey][State.indexByKey] is rebuilt wholesale on insert (fine at these
     * sizes). In-place *updates* to an already-existing item go through plain [upsert] instead -
     * this helper is for the CREATE path only. [ChatItem.ErrorCard] ([foldCompletion])
     * intentionally never routes through here - it always appends at the very end.
     */
    private fun appendOrInsertBeforeOpenAssistant(state: State, item: ChatItem): State {
        val assistantIndex = state.openAssistantKey?.let { state.indexByKey[it] } ?: return upsert(state, item)
        val items = state.items.toMutableList().apply { add(assistantIndex, item) }
        return state.copy(items = items, indexByKey = items.mapIndexed { i, it -> it.key to i }.toMap())
    }

    private fun appendDelta(state: State, delta: String, ts: String): State {
        val opened = openAssistantIfNeeded(state, ts)
        val buffer = opened.assistantBuffer + delta
        val key = requireNotNull(opened.openAssistantKey)
        val updated = upsert(opened, ChatItem.AssistantMessage(text = buffer, isStreaming = true, ts = ts, key = key))
        return updated.copy(assistantBuffer = buffer)
    }

    private fun replaceBuffer(state: State, text: String, ts: String): State {
        val opened = openAssistantIfNeeded(state, ts)
        val key = requireNotNull(opened.openAssistantKey)
        val updated = upsert(opened, ChatItem.AssistantMessage(text = text, isStreaming = true, ts = ts, key = key))
        return updated.copy(assistantBuffer = text)
    }

    private fun finalizeAssistant(state: State, text: String, ts: String): State {
        val opened = openAssistantIfNeeded(state, ts)
        val key = requireNotNull(opened.openAssistantKey)
        val finalized = upsert(opened, ChatItem.AssistantMessage(text = text, isStreaming = false, ts = ts, key = key))
        return finalized.copy(openAssistantKey = null, assistantBuffer = "")
    }

    private fun closeStreaming(state: State): State {
        val key = state.openAssistantKey ?: return state
        val current = state.items[state.indexByKey.getValue(key)] as ChatItem.AssistantMessage
        val closed = upsert(state, current.copy(isStreaming = false))
        return closed.copy(openAssistantKey = null, assistantBuffer = "")
    }

    /**
     * Appends into the currently-open [ChatItem.ToolCallGroup] (consecutive `tool_result`s in one
     * turn fold into one card), or opens a new one keyed by THIS call when none is open - either
     * because it's the first `tool_result` ever, or [closeToolGroup] cleared the key at the last
     * user turn (see that call site). [upsert]'s existing-key branch replaces the group in place,
     * so the key stays stable across every later append; a brand-new group instead goes through
     * [appendOrInsertBeforeOpenAssistant] so a `tool_result` that arrives mid-narration still
     * lands ahead of the turn's assistant text (activity-precedes-narration, file header).
     */
    private fun foldToolResult(state: State, event: SessionEvent.ToolResult): State {
        val payload = event.payload
        val call = ToolCall(
            toolId = payload.toolId,
            operation = payload.operation,
            inputJson = payload.toolInput,
            summary = payload.summary,
            success = payload.success ?: (payload.error == null),
            detail = payload.result,
            error = payload.error,
            model = payload.model,
            ts = event.ts,
            key = "tool_result:${event.ts}:${payload.toolId}",
        )
        val groupKey = state.openToolGroupKey ?: call.key
        val existingGroup = state.indexByKey[groupKey]?.let { state.items[it] } as? ChatItem.ToolCallGroup
        val group = ChatItem.ToolCallGroup(calls = (existingGroup?.calls ?: emptyList()) + call, key = groupKey)
        val folded = if (existingGroup != null) upsert(state, group) else appendOrInsertBeforeOpenAssistant(state, group)
        return folded.copy(openToolGroupKey = groupKey)
    }

    /**
     * Only a genuine run FAILURE surfaces a [ChatItem.ErrorCard] - the completion payload's own
     * `error` field, which the backend sets solely on the orchestrator's terminal-failure path.
     * `lastError` alone (a residual from an earlier, already-recovered tool failure inside an
     * otherwise-successful run) must never spawn a card under a successful chat; when `error` IS
     * present the rendered message still prefers it, falling back to `lastError` only as a
     * belt-and-suspenders default. This card always appends at the very end, never through
     * [appendOrInsertBeforeOpenAssistant] - it is not a per-turn activity item.
     */
    private fun foldCompletion(state: State, event: SessionEvent.Completion): State {
        var next = closeStreaming(state)
        val error = event.payload.error
        if (error != null) {
            val message = if (error.isNotEmpty()) error else (event.payload.lastError ?: error)
            next = upsert(next, ChatItem.ErrorCard(message = message, ts = event.ts, key = "error:${event.ts}"))
        }
        return next
    }

    private fun foldTodos(state: State, event: SessionEvent.Todos): State {
        val items = event.payload.items.map { ChatTodoItem(label = it.label, status = it.status) }
        val todoList = ChatItem.TodoList(items = items, ts = event.ts, key = "todos")
        return if (state.indexByKey["todos"] == null) appendOrInsertBeforeOpenAssistant(state, todoList) else upsert(state, todoList)
    }

    /**
     * Upserts one [ChatItem.AgentChip] per `agentId` (Rev D §D-5) - the first `sub_agent` event
     * for an id creates the chip, every later one for the SAME id updates it in place via [upsert].
     * `status` falls back to `action` when the backend hasn't attached a status yet (spec: "label =
     * {agentType-or-shortId} · {status-or-action}"); an unrecognized status/action value is
     * tolerated - it just renders as non-terminal, in-progress text, never dropped or crashed on.
     */
    private fun foldSubAgent(state: State, event: SessionEvent.SubAgent): State {
        val payload = event.payload
        val status = payload.status ?: payload.action
        val terminal = status.lowercase() in TERMINAL_AGENT_STATUSES
        val success = if (terminal) status.equals("completed", ignoreCase = true) else null
        val key = "agent:${payload.agentId}"
        val chip = ChatItem.AgentChip(
            agentId = payload.agentId,
            agentType = payload.agentType,
            status = status,
            stepsCompleted = payload.stepsCompleted,
            terminal = terminal,
            success = success,
            ts = event.ts,
            key = key,
        )
        return if (state.indexByKey[key] == null) appendOrInsertBeforeOpenAssistant(state, chip) else upsert(state, chip)
    }

    private fun withinEchoWindow(aTs: String, bTs: String): Boolean {
        val a = Timestamps.parseInstantOrNull(aTs) ?: return false
        val b = Timestamps.parseInstantOrNull(bTs) ?: return false
        return abs(Duration.between(a, b).seconds) <= USER_ECHO_WINDOW_SECONDS
    }

    private const val USER_ECHO_WINDOW_SECONDS = 30L

    /** Terminal `sub_agent` status/action values (spec D-5); `completed` is the only success one. */
    private val TERMINAL_AGENT_STATUSES = setOf("completed", "failed", "cancelled", "rejected")
}
