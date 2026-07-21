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
        /** Key of the [ChatItem.ToolCallGroup] still accepting consecutive `tool_result`s
         * - cleared ONLY when a fresh user/user_steer turn begins ([foldUserText]), so
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
            // The ONLY thing separating these two: `user_steer` was enqueued INTO a running run, and
            // the backend's retry scan is blind to that type (ChatItem.UserBubble.steer). Both still
            // render as the same bubble.
            is SessionEvent.User -> foldUserText(deduped, event.payload, event.ts, pending, steer = false)
            is SessionEvent.UserSteer -> foldUserText(deduped, event.payload, event.ts, pending, steer = true)
            is SessionEvent.AgentMessageDelta ->
                if (event.payload.depth != 0) deduped else appendDelta(deduped, event.payload.text, event.ts)
            is SessionEvent.AgentMessage ->
                if (event.payload.depth != 0) deduped else replaceBuffer(deduped, event.payload.text, event.ts)
            is SessionEvent.Assistant -> finalizeAssistant(deduped, event.payload.text, event.ts)
            is SessionEvent.ToolResult -> foldToolResult(deduped, event)
            is SessionEvent.Completion -> foldCompletion(deduped, event)
            is SessionEvent.Todos -> foldTodos(deduped, event)
            is SessionEvent.SubAgent -> foldSubAgent(deduped, event)
            is SessionEvent.WidgetReady -> foldWidget(deduped, event)
            is SessionEvent.UserQuestion -> foldUserQuestion(deduped, event)
            is SessionEvent.UserQuestionAnswered -> foldUserQuestionAnswered(deduped, event)
            is SessionEvent.StreamEnd -> closeStreaming(deduped)
            is SessionEvent.LlmCallEnd,
            is SessionEvent.Permission,
            is SessionEvent.DeviceToolCall,
            is SessionEvent.StreamError, // intercepted by ChatViewModel before reaching fold(); never rendered
            is SessionEvent.Unknown,
            // Build-terminal signal for the Mewbo Apps creation flow - AppCreateViewModel follows
            // RunRepository.live() directly and reacts to it there, the same way DeviceToolCall
            // never reaches the chat transcript either.
            is SessionEvent.AppReady,
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
    private fun foldUserText(state: State, payload: TextPayload, ts: String, pending: Boolean, steer: Boolean): State {
        val text = payload.text
        val closed = closeToolGroup(state)
        val existing = closed.items.firstOrNull { item ->
            item is ChatItem.UserBubble && item.text == text && withinEchoWindow(item.ts, ts)
        } as? ChatItem.UserBubble
        if (existing != null) {
            // The server's event is the AUTHORITY on this bubble, and this branch is the only place
            // the client ever hears it - so [ts] and [steer] are both adopted from it here, not just
            // `pending` cleared.
            //
            // **[ts] is the load-bearing one.** A bubble born from the client's optimistic echo
            // (ChatViewModel.send) carries `Instant.now()` - a LOCAL clock stamp that exists nowhere
            // in the server's transcript. Leaving it in place made every message sent in the current
            // session unusable as a `from_ts` anchor: `/recover` would 400 ("no user event at ts=…")
            // and `/fork` would cut the transcript at a timestamp that means nothing in it. The row's
            // [ChatItem.key] deliberately does NOT follow - it stays whatever it was created as, so
            // LazyColumn identity (and the row's animation state) survives the swap. Key = identity;
            // ts = the server anchor. They happen to be derived from the same value at creation, and
            // that coincidence is exactly what hid this.
            //
            // **[steer]** likewise: the optimistic echo of a STEERING send is folded as a
            // SessionEvent.User, so it is born `steer = false` regardless of what it really was. The
            // server's `user_steer` is the first authoritative word, and it arrives right here -
            // dropping it would leave every steered bubble claiming it can anchor a retry.
            //
            // Unconditional (not gated on `existing.pending` as it once was): a NON-pending optimistic
            // echo - i.e. every ordinary fresh-turn send - has exactly the same wrong ts, and was
            // silently skipping this reconciliation entirely.
            return upsert(closed, existing.copy(ts = ts, pending = false, steer = steer))
        }
        val attachments = payload.attachments?.map {
            AttachmentSummary(filename = it.filename, mimeType = it.contentType, sizeBytes = it.sizeBytes)
        } ?: emptyList()
        return upsert(
            closed,
            // The key deliberately does NOT encode `steer` - it is the same message either way, and a
            // key change would re-animate the row when the echo reconciles.
            ChatItem.UserBubble(text = text, ts = ts, pending = pending, attachments = attachments, steer = steer, key = "user:$ts:${text.hashCode()}"),
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
     *
     * **Promotion gate**: a call for which [PromotedTools.isPromoted] AND [ToolCall.success] both
     * hold skips the group entirely, routing to [foldPromotedToolCard] instead. `success` is part
     * of the gate deliberately - the group fold already renders failures faithfully, and a
     * confident, singled-out action card for a FAILED promoted call (e.g. an alarm that didn't get
     * set) would misrepresent what happened - so a failed promoted call falls through to this same
     * generic group path unchanged.
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
        if (PromotedTools.isPromoted(call.toolId) && call.success) return foldPromotedToolCard(state, call)
        val groupKey = state.openToolGroupKey ?: call.key
        val existingGroup = state.indexByKey[groupKey]?.let { state.items[it] } as? ChatItem.ToolCallGroup
        val group = ChatItem.ToolCallGroup(calls = (existingGroup?.calls ?: emptyList()) + call, key = groupKey)
        val folded = if (existingGroup != null) upsert(state, group) else appendOrInsertBeforeOpenAssistant(state, group)
        return folded.copy(openToolGroupKey = groupKey)
    }

    /**
     * Promoted-tool path ([PromotedTools]): a successful allowlisted call becomes its own
     * top-level [ChatItem.ToolCard] instead of folding into the turn's [ChatItem.ToolCallGroup].
     * Deliberately leaves [State.openToolGroupKey] untouched - a promoted card is orthogonal to
     * the turn's tool-group fold, so unregistered `tool_result`s arriving before or after it keep
     * appending to whatever group (if any) is already open, and the group's call count never
     * counts the promoted call. Idempotent the same way [foldToolResult] is: an existing key
     * upserts in place; a new one inserts ahead of the turn's already-open assistant text
     * (activity-precedes-narration, file header).
     */
    private fun foldPromotedToolCard(state: State, call: ToolCall): State {
        // Namespaced, like the sibling singleton paths ("todos", "agent:…"), NOT the bare call.key:
        // a fresh ToolCallGroup adopts its FIRST call's key verbatim, so a bare key here shares a
        // namespace with the group. The existence check below is type-blind, so a collision would
        // upsert a ToolCard over a live group while openToolGroupKey still pointed at it - and the
        // next unregistered tool_result would then append a duplicate key, which LazyColumn throws on.
        // Unreachable today (it needs a failed and a successful promoted call at an identical ts, and
        // the backend stamps microseconds), but the namespace costs nothing and the class of bug is real.
        val card = ChatItem.ToolCard(call = call, key = "toolcard:${call.key}")
        return if (state.indexByKey[card.key] != null) upsert(state, card) else appendOrInsertBeforeOpenAssistant(state, card)
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
     * A `widget_ready` event becomes its own top-level [ChatItem.Widget], keyed by
     * `widget_id` so a re-emitted widget upserts in place rather than stacking. Like the promoted
     * tool card ([foldPromotedToolCard]) it is the visible payload of a turn, so it routes through
     * [appendOrInsertBeforeOpenAssistant] on first arrival - inheriting activity-precedes-narration
     * for free - and does NOT touch [State.openToolGroupKey] (orthogonal to the tool-group fold).
     */
    private fun foldWidget(state: State, event: SessionEvent.WidgetReady): State {
        val payload = event.payload
        val widget = ChatItem.Widget(
            widgetId = payload.widgetId,
            sessionId = payload.sessionId,
            appPy = payload.files.appPy,
            dataJson = payload.files.dataJson,
            requirements = payload.requirements,
            summary = payload.summary,
            ts = event.ts,
            key = "widget:${payload.widgetId}",
        )
        return if (state.indexByKey[widget.key] == null) appendOrInsertBeforeOpenAssistant(state, widget) else upsert(state, widget)
    }

    /**
     * A `user_question` event (core `ask_user_question`, which BLOCKS the run) becomes its own
     * top-level [ChatItem.Question], keyed by `call_id`, created PENDING (`resolution = null`). Like
     * the widget/promoted card it is the visible payload of a turn, so it routes through
     * [appendOrInsertBeforeOpenAssistant] on first arrival (activity-precedes-narration) and does NOT
     * touch [State.openToolGroupKey] (orthogonal to the tool-group fold). A re-emitted question upserts
     * in place. The answer SHAPE (free-text/single/multi) is derived by the card from
     * [UiQuestion.multiSelect] + option count — never stored here, so it can't drift.
     */
    private fun foldUserQuestion(state: State, event: SessionEvent.UserQuestion): State {
        val payload = event.payload
        val question = ChatItem.Question(
            callId = payload.callId,
            callToken = payload.callToken,
            questions = payload.questions.map { spec ->
                UiQuestion(
                    header = spec.header,
                    question = spec.question,
                    options = spec.options.map { UiQuestionOption(label = it.label, description = it.description) },
                    multiSelect = spec.multiSelect,
                )
            },
            resolution = null,
            ts = event.ts,
            key = "question:${payload.callId}",
        )
        return if (state.indexByKey[question.key] == null) appendOrInsertBeforeOpenAssistant(state, question) else upsert(state, question)
    }

    /**
     * A `user_question_answered` event settles the matching [ChatItem.Question] (by `call_id`) — even
     * when a DIFFERENT surface answered (the whole point of the event). `answered` carries the chosen
     * answers; every other outcome (`declined`/`interrupted`/`cancelled`, or any unknown future value)
     * is a plain [QuestionResolution.Dismissed] with no error residue. A matching pending card is
     * required: an answered event with no prior question (never happens in a well-formed, ts-ordered
     * log — the `user_question` always precedes) is ignored rather than rendering an answer with no
     * question. The event's content-key dedupe ([fold]) means a replayed `user_question` never resets
     * a card this already settled.
     */
    private fun foldUserQuestionAnswered(state: State, event: SessionEvent.UserQuestionAnswered): State {
        val payload = event.payload
        val key = "question:${payload.callId}"
        val existing = state.indexByKey[key]?.let { state.items[it] } as? ChatItem.Question ?: return state
        val resolution = if (payload.outcome == "answered") {
            QuestionResolution.Answered(
                answers = (payload.answers ?: emptyList()).map { UiAnswer(selectedIndexes = it.selectedIndexes, text = it.text) },
                answeredVia = payload.answeredVia,
            )
        } else {
            QuestionResolution.Dismissed(outcome = payload.outcome)
        }
        return upsert(state, existing.copy(resolution = resolution))
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
