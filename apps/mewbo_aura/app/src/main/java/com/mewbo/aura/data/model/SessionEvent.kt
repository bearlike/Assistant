package com.mewbo.aura.data.model

import kotlinx.serialization.DeserializationStrategy
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.descriptors.buildClassSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonContentPolymorphicSerializer
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive

/**
 * Wire shape (verified against `mewbo_api`/`mewbo_core` source, scratchpad/api-contract.md):
 * every session event frame is `{"type": string, "ts": ISO-8601, "payload": {...}}`, except the
 * SSE-only terminal control frame `{"type":"stream_end"}` which carries no `ts`/`payload`.
 *
 * There is deliberately NO `type == "error"` branch here: grepping the session-transcript code
 * paths turned up zero emitters of that type on this event family (it only exists on the
 * separate wiki/agentic-search SSE logs). Failure is instead surfaced through [Completion]
 * (`error`/`last_error`), [ToolResult] (`success == false`), and [LlmCallEnd]
 * (`success == false`) - see api-contract.md section 6. Callers must not switch on a generic
 * error type; it will never arrive.
 */
sealed interface SessionEvent {
    val ts: String

    @Serializable
    data class User(override val ts: String, val payload: TextPayload) : SessionEvent

    /** A steering message enqueued into an already-active run (`/message` while `running`). */
    @Serializable
    data class UserSteer(override val ts: String, val payload: TextPayload) : SessionEvent

    /** Token-level incremental text; the only source of partial/incremental rendering. */
    @Serializable
    data class AgentMessageDelta(override val ts: String, val payload: AgentMessageDeltaPayload) : SessionEvent

    /** Authoritative text for the in-progress step; REPLACES the accumulated delta buffer. */
    @Serializable
    data class AgentMessage(override val ts: String, val payload: AgentMessagePayload) : SessionEvent

    /** Final assistant reply for the turn; exactly one per turn, even on failure. */
    @Serializable
    data class Assistant(override val ts: String, val payload: TextPayload) : SessionEvent

    @Serializable
    data class ToolResult(override val ts: String, val payload: ToolResultPayload) : SessionEvent

    /** Terminal-for-the-turn marker. */
    @Serializable
    data class Completion(override val ts: String, val payload: CompletionPayload) : SessionEvent

    /** Authoritative live checklist - re-emitted in FULL on every occurrence, never a patch. */
    @Serializable
    data class Todos(override val ts: String, val payload: TodosPayload) : SessionEvent

    @Serializable
    data class LlmCallEnd(override val ts: String, val payload: LlmCallEndPayload) : SessionEvent

    @Serializable
    data class SubAgent(override val ts: String, val payload: SubAgentPayload) : SessionEvent

    @Serializable
    data class Permission(override val ts: String, val payload: PermissionPayload) : SessionEvent

    /** A client-executable tool call dispatched to this device - resolved by
     * [com.mewbo.aura.data.device.DeviceToolExecutor] posting a result keyed on
     * [DeviceToolCallPayload.callToken]. Never rendered in the chat transcript
     * ([TranscriptReducer] drops it the same as `llm_call_end`/`permission`). */
    @Serializable
    data class DeviceToolCall(override val ts: String, val payload: DeviceToolCallPayload) : SessionEvent

    /** A Streamlit widget the model built via the `widget_builder` plugin. Carries
     * the whole `app.py` + `data.json` bundle the client renders in a self-contained stlite WebView
     * ([com.mewbo.aura.ui.chat.widget.WidgetCard]); folded into a [ChatItem.Widget] keyed by
     * [WidgetReadyPayload.widgetId]. Only arrives when the client advertised the `stlite` capability
     * (see data/CLAUDE.md § "Widget capability"). */
    @Serializable
    data class WidgetReady(override val ts: String, val payload: WidgetReadyPayload) : SessionEvent

    /** Build-phase terminal event for the Mewbo Apps sub-product (design spec §3 "Wire events"):
     * fired once the `submit_app` tool validates + persists a builder run's [AppSpec][com.mewbo.aura.data.model.AppSummary],
     * mirroring how [WidgetReady] terminates a widget build. [com.mewbo.aura.ui.apps.AppCreateViewModel]
     * follows [com.mewbo.aura.data.repo.RunRepository.live] for the creation session looking for this
     * event to know when to navigate to the freshly-live app's detail screen. Only arrives when the
     * client advertised the `apps` capability (see `di/DataModule.kt`'s `AuthInterceptor`). */
    @Serializable
    data class AppReady(override val ts: String, val payload: AppReadyPayload) : SessionEvent

    /**
     * A pending ask-user question group (core `ask_user_question` tool, which BLOCKS the run). Carries
     * the 1-4 questions plus the single-use [UserQuestionPayload.callToken] the answer POST presents
     * ([com.mewbo.aura.data.api.AuraApi.answerQuestion]). Folded into a [ChatItem.Question] card keyed
     * by [UserQuestionPayload.callId]; settled by the matching [UserQuestionAnswered] event (even when
     * another surface answered). Only arrives when the client advertised the `ask_user` capability
     * (`X-Mewbo-Capabilities`, see [com.mewbo.aura.di.AuthInterceptor]).
     */
    @Serializable
    data class UserQuestion(override val ts: String, val payload: UserQuestionPayload) : SessionEvent

    /**
     * Resolution of a [UserQuestion] group — records the outcome so EVERY surface (not only the one
     * that answered) settles its card. [UserQuestionAnsweredPayload.outcome] is `answered` (carrying
     * the chosen answers) or `timed_out`/`declined`/`interrupted`/`cancelled` (the RUN stopped
     * waiting - NOT that the question is resolved). Only `answered` settles the card read-only; every
     * other outcome, including an unknown future value, keeps it tappable so a late human answer can
     * still land ([TranscriptReducer]).
     */
    @Serializable
    data class UserQuestionAnswered(override val ts: String, val payload: UserQuestionAnsweredPayload) : SessionEvent

    /** SSE-only terminal control frame. No `ts` on the wire; not part of the persisted transcript. */
    @Serializable
    data object StreamEnd : SessionEvent {
        override val ts: String = ""
    }

    /**
     * Client-synthesized, never decoded from wire JSON (no `"type"` value maps to it, deliberately
     * NOT `@Serializable`) - constructed directly by [com.mewbo.aura.data.repo.RunRepository]'s
     * `live()` when the underlying SSE upstream throws (review finding F2). Once a cold upstream
     * flow is wrapped in `shareIn`, an exception it throws executes inside the sharing coroutine on
     * an app-wide scope with no installed exception handler - materializing it into a VALUE here is
     * what lets [com.mewbo.aura.ui.chat.ChatViewModel] route it through its own client-error path
     * instead of it surfacing as an uncaught exception (process kill on Android).
     */
    data class StreamError(override val ts: String = "", val message: String) : SessionEvent

    /** Forward-compat catch-all so a brand new backend event type never crashes parsing. */
    data class Unknown(val type: String, override val ts: String, val raw: JsonElement) : SessionEvent

    companion object {
        /** Resilient decode: any malformed/unrecognized frame degrades to [Unknown], never throws. */
        fun decode(json: Json, raw: String): SessionEvent = try {
            decode(json, json.parseToJsonElement(raw))
        } catch (e: SerializationException) {
            Unknown(type = "unparseable", ts = "", raw = JsonNull)
        } catch (e: IllegalArgumentException) {
            Unknown(type = "unparseable", ts = "", raw = JsonNull)
        }

        fun decode(json: Json, element: JsonElement): SessionEvent = try {
            json.decodeFromJsonElement(SessionEventSerializer, element)
        } catch (e: SerializationException) {
            toUnknown(element)
        } catch (e: IllegalArgumentException) {
            toUnknown(element)
        }

        private fun toUnknown(element: JsonElement): Unknown {
            val obj = element as? JsonObject
            val type = obj?.get("type")?.jsonPrimitive?.content ?: "unknown"
            val ts = obj?.get("ts")?.jsonPrimitive?.content ?: ""
            return Unknown(type = type, ts = ts, raw = element)
        }

        /**
         * `true` when [event] is a persisted `context` frame. `context` isn't its own
         * [SessionEvent] variant - the dispatcher has no `"context"` branch (see the class doc's
         * "there is deliberately no error branch" note for the same pattern) - so every context
         * frame decodes to [Unknown] with `type == "context"`, and this is the ONE place that
         * fact is spelled.
         *
         * Public because a context event is the durable record of a session's PROJECT, and the
         * project now moves mid-run: the model can call `switch_project`, which persists a fresh
         * `context` event naming the project it moved to. A caller folding events one at a time
         * needs to tell "a context event that names no project" (the session is on a temp-dir cwd)
         * apart from "not a context event" (nothing to adopt) - a bare `String?` from
         * [contextProject] collapses the two, so pair it with this.
         */
        fun isContextEvent(event: SessionEvent): Boolean = event is Unknown && event.type == "context"

        /** [event]'s own payload object when it is a `context` frame, else `null`. */
        private fun contextPayload(event: SessionEvent): JsonObject? =
            if (isContextEvent(event)) ((event as Unknown).raw as? JsonObject)?.get("payload") as? JsonObject else null

        /**
         * The MOST RECENT persisted `context` event's payload object, per the backend's own
         * `_load_last_context` semantics (backend.py): the latest context event wins VERBATIM,
         * never merged across several. The ONE reverse-scan the field readers below share; `null`
         * when the session has no context event at all.
         */
        private fun lastContextPayload(events: List<SessionEvent>): JsonObject? {
            for (event in events.asReversed()) {
                if (isContextEvent(event)) return contextPayload(event)
            }
            return null
        }

        /** One context payload's `project`, blank treated as absent. `as? JsonPrimitive`, never the
         * `jsonPrimitive` accessor: that one THROWS on a nested object/array, and this now runs on
         * the live per-event path where an exception would take the run's collector down rather
         * than landing in a history load's own try/catch. */
        private fun projectOf(payload: JsonObject?): String? =
            (payload?.get("project") as? JsonPrimitive)?.content?.takeIf { it.isNotBlank() }

        /**
         * The model an EXISTING session's next turn would actually run on: the most recent `context`
         * event's `model` field ([lastContextPayload]). `null` when the session has no context event,
         * or its most recent one has no `model` field (mirrors `context_payload.get("model")` being
         * absent server-side).
         */
        fun lastContextModel(events: List<SessionEvent>): String? =
            (lastContextPayload(events)?.get("model") as? JsonPrimitive)?.content?.takeIf { it.isNotBlank() }

        /**
         * The project ([ProjectSummary.contextKey] - bare name or `managed:<id>`) an EXISTING
         * session's next turn resolves against: the most recent `context` event's `project` field.
         * Round-trips the value the app itself last sent (backend `_build_context_payload` copies the
         * whole context object verbatim into the persisted event). `null` => the session is scoped to
         * Temporary (no project), which is an honest match of what the backend re-resolves - so a
         * revisited session's frozen scope reflects its REAL project, not the app-wide default.
         *
         * Since the model can move the session itself (`switch_project` persists a `context` event
         * naming the project it moved to), "the most recent context event" is no longer only the
         * app's own last send — it is genuinely where the session ended up. [contextProject] is the
         * per-event sibling that keeps that live during a running turn.
         */
        fun lastContextProject(events: List<SessionEvent>): String? = projectOf(lastContextPayload(events))

        /**
         * The project ONE `context` event names — the per-event sibling of [lastContextProject],
         * for a caller folding events as they stream in rather than reading a settled transcript.
         * `null` for a context event carrying no `project` (the session is on a temp-dir cwd) AND
         * for an event that isn't a context frame at all, so gate on [isContextEvent] first;
         * [adoptContextProject] does exactly that and is what callers should reach for.
         */
        fun contextProject(event: SessionEvent): String? = projectOf(contextPayload(event))

        /**
         * The session's project after [event]: the event's own `project` when it IS a `context`
         * frame, otherwise [current] unchanged.
         *
         * This is the whole mid-run project-switch seam, as a total pure function. A run that
         * switches project emits a `context` event for it, so folding EVERY event through here
         * keeps "which project is this session in" live for the length of a turn, instead of a
         * fact read once at session load. Also correct on history replay: applying each context
         * event in order lands on exactly what [lastContextProject] would have returned.
         */
        fun adoptContextProject(event: SessionEvent?, current: String?): String? =
            if (event != null && isContextEvent(event)) contextProject(event) else current

        /**
         * The tool allowlist an EXISTING session's next turn narrows to: the most recent `context`
         * event's `mcp_tools` array. THREE-STATE, and this is the READ half of the same law
         * [ComposerScope.mcpToolsForContext] writes - the two must agree or a session cannot
         * round-trip its own ceiling:
         * - key ABSENT (or not an array) => `null`, untouched, and the next `/query` omits it again
         *   so the backend binds every tool.
         * - key present and EMPTY => the empty set, an explicit ceiling of zero that the next
         *   `/query` re-declares verbatim.
         * - non-empty => exactly those ids.
         *
         * Collapsing the empty case into `null` here re-opens the fail-open from the WRITE side
         * one turn later: re-opening an all-tools-off session would hydrate it as untouched and the
         * very next send would re-bind the whole registry, with nothing in the UI to show it.
         */
        fun lastContextMcpTools(events: List<SessionEvent>): Set<String>? {
            val raw = lastContextPayload(events)?.get("mcp_tools") as? JsonArray ?: return null
            return raw.mapNotNull { (it as? JsonPrimitive)?.content?.takeIf(String::isNotBlank) }.toSet()
        }
    }
}

/** Dispatches on the envelope's `type` field; [SessionEvent.decode] wraps this with a try/catch net. */
private object SessionEventSerializer : JsonContentPolymorphicSerializer<SessionEvent>(SessionEvent::class) {
    override fun selectDeserializer(element: JsonElement): DeserializationStrategy<SessionEvent> {
        val type = (element as? JsonObject)?.get("type")?.jsonPrimitive?.content
        return when (type) {
            "user" -> SessionEvent.User.serializer()
            "user_steer" -> SessionEvent.UserSteer.serializer()
            "agent_message_delta" -> SessionEvent.AgentMessageDelta.serializer()
            "agent_message" -> SessionEvent.AgentMessage.serializer()
            "assistant" -> SessionEvent.Assistant.serializer()
            "tool_result" -> SessionEvent.ToolResult.serializer()
            "completion" -> SessionEvent.Completion.serializer()
            "todos" -> SessionEvent.Todos.serializer()
            "llm_call_end" -> SessionEvent.LlmCallEnd.serializer()
            "sub_agent" -> SessionEvent.SubAgent.serializer()
            "permission" -> SessionEvent.Permission.serializer()
            "device_tool_call" -> SessionEvent.DeviceToolCall.serializer()
            "widget_ready" -> SessionEvent.WidgetReady.serializer()
            "app_ready" -> SessionEvent.AppReady.serializer()
            "user_question" -> SessionEvent.UserQuestion.serializer()
            "user_question_answered" -> SessionEvent.UserQuestionAnswered.serializer()
            "stream_end" -> SessionEvent.StreamEnd.serializer()
            else -> UnknownEventSerializer
        }
    }
}

private object UnknownEventSerializer : KSerializer<SessionEvent.Unknown> {
    override val descriptor: SerialDescriptor =
        buildClassSerialDescriptor("com.mewbo.aura.data.model.SessionEvent.Unknown")

    override fun deserialize(decoder: Decoder): SessionEvent.Unknown {
        val jsonDecoder = decoder as? JsonDecoder
            ?: throw SerializationException("SessionEvent.Unknown only supports JSON decoding")
        val element = jsonDecoder.decodeJsonElement()
        val obj = element as? JsonObject
        val type = obj?.get("type")?.jsonPrimitive?.content ?: "unknown"
        val ts = obj?.get("ts")?.jsonPrimitive?.content ?: ""
        return SessionEvent.Unknown(type = type, ts = ts, raw = element)
    }

    override fun serialize(encoder: Encoder, value: SessionEvent.Unknown) {
        throw SerializationException("SessionEvent.Unknown is decode-only")
    }
}

// ---- Payload shapes (nested under the envelope's "payload" key) --------------------------

/**
 * [attachments] rides BOTH the wire shape (the backend now puts an `attachments` array on the
 * persisted `user`/`user_steer` event payload - field-for-field with
 * [com.mewbo.aura.data.api.AttachmentRecordDto], `ignoreUnknownKeys` tolerating the extra
 * `id`/`stored_name`/`uploaded_at`/`parsed` fields this decode has no use for) AND the client's own
 * synthetic optimistic echo ([com.mewbo.aura.ui.chat.ChatViewModel.send] maps its staged
 * attachments into the same shape before folding) - one payload shape for both origins, same as
 * [text] already is. `null`/absent on every OTHER `TextPayload` consumer ([SessionEvent.Assistant]
 * never carries attachments).
 */
@Serializable
data class TextPayload(val text: String, val attachments: List<AttachmentPayload>? = null)

/** Wire shape for one attachment riding a `user`/`user_steer` event (see [TextPayload]'s KDoc). */
@Serializable
data class AttachmentPayload(
    val filename: String,
    @SerialName("content_type") val contentType: String,
    @SerialName("size_bytes") val sizeBytes: Long? = null,
)

@Serializable
data class AgentMessagePayload(
    val text: String,
    @SerialName("agent_id") val agentId: String,
    val depth: Int = 0,
)

@Serializable
data class AgentMessageDeltaPayload(
    val text: String,
    @SerialName("agent_id") val agentId: String,
    val depth: Int = 0,
    val step: Int? = null,
)

@Serializable
data class ToolResultPayload(
    @SerialName("tool_id") val toolId: String,
    val operation: String,
    @SerialName("tool_input") val toolInput: JsonElement? = null,
    val result: String? = null,
    val success: Boolean? = null,
    val summary: String? = null,
    val error: String? = null,
    @SerialName("result_file") val resultFile: String? = null,
    @SerialName("agent_id") val agentId: String? = null,
    val model: String? = null,
)

@Serializable
data class CompletionPayload(
    val done: Boolean = true,
    @SerialName("done_reason") val doneReason: String? = null,
    @SerialName("task_result") val taskResult: String? = null,
    val error: String? = null,
    @SerialName("last_error") val lastError: String? = null,
)

@Serializable
data class TodoItem(val label: String, val status: String)

@Serializable
data class TodosPayload(
    val items: List<TodoItem> = emptyList(),
    val source: String? = null,
    @SerialName("agent_id") val agentId: String? = null,
)

@Serializable
data class LlmCallEndPayload(
    @SerialName("agent_id") val agentId: String? = null,
    val depth: Int = 0,
    val step: Int? = null,
    val success: Boolean = true,
    val model: String? = null,
    @SerialName("error_type") val errorType: String? = null,
    val reason: String? = null,
)

@Serializable
data class SubAgentPayload(
    val action: String,
    @SerialName("agent_id") val agentId: String,
    @SerialName("parent_id") val parentId: String? = null,
    val depth: Int = 0,
    val model: String? = null,
    val detail: String? = null,
    val status: String? = null,
    @SerialName("steps_completed") val stepsCompleted: Int? = null,
    @SerialName("input_tokens") val inputTokens: Int? = null,
    @SerialName("output_tokens") val outputTokens: Int? = null,
    @SerialName("agent_type") val agentType: String? = null,
    val summary: String? = null,
)

@Serializable
data class PermissionPayload(
    @SerialName("tool_id") val toolId: String,
    val operation: String,
    @SerialName("tool_input") val toolInput: JsonElement? = null,
    val decision: String,
)

/** Wire shape verbatim: `call_token` is consumed exactly once by the
 * result POST (403 bad token / 404 unknown / 409 already-consumed); `expires_at` is epoch
 * seconds as a float, compared directly against [com.mewbo.aura.data.device.DeviceClock]. */
@Serializable
data class DeviceToolCallPayload(
    @SerialName("call_id") val callId: String,
    @SerialName("call_token") val callToken: String,
    @SerialName("tool_id") val toolId: String,
    val args: JsonObject = JsonObject(emptyMap()),
    @SerialName("expires_at") val expiresAt: Double,
)

/**
 * Wire shape of a `widget_ready` event (mirrors `mewbo_core`'s `WidgetReadyPayload`
 * and the console `WidgetReadyPayload` type field-for-field). [files] MUST carry both `app.py` and
 * `data.json` ([WidgetFiles] makes them required, so a malformed bundle decodes to
 * [SessionEvent.Unknown] and is dropped rather than rendered half-formed). The whole payload is
 * re-serialized verbatim into the WebView's `postMessage` — the stlite host page owns booting it.
 */
@Serializable
data class WidgetReadyPayload(
    @SerialName("widget_id") val widgetId: String,
    @SerialName("session_id") val sessionId: String,
    val files: WidgetFiles,
    val requirements: List<String> = emptyList(),
    val summary: String? = null,
)

/** The two files every widget bundle ships (`app.py` is the Streamlit script, `data.json` its
 * inlined data). Both required — see [WidgetReadyPayload]. */
@Serializable
data class WidgetFiles(
    @SerialName("app.py") val appPy: String,
    @SerialName("data.json") val dataJson: String,
)

/** Wire shape of an `app_ready` event (Mewbo Apps design spec §3, "Wire events" — frozen literal
 * `{app_id, title, summary, version}`). Mirrors [WidgetReadyPayload]'s role for the widget flow. */
@Serializable
data class AppReadyPayload(
    @SerialName("app_id") val appId: String,
    val title: String,
    val summary: String,
    val version: Int,
)

/**
 * Wire shape of a `user_question` event (core `ask_user.py`'s `USER_QUESTION_EVENT`, snake_case).
 * [callToken] is a single-use bearer secret the answer POST presents (403 on mismatch) — proof of
 * stream-read access, NOT of which surface answers. [questions] holds 1-4 questions; an empty
 * [UserQuestionSpec.options] list is a free-text question. [timeoutSeconds] is surfaced understatedly
 * on the card, never as a live countdown; [notesPlaceholder], when present, adds ONE group-level
 * free-text field (separate from each question's own "Other") whose value posts as the answer
 * request's `notes` — blank input omits the field entirely, never an empty string on the wire.
 */
@Serializable
data class UserQuestionPayload(
    @SerialName("call_id") val callId: String,
    @SerialName("call_token") val callToken: String,
    val questions: List<UserQuestionSpec> = emptyList(),
    @SerialName("timeout_seconds") val timeoutSeconds: Int? = null,
    @SerialName("notes_placeholder") val notesPlaceholder: String? = null,
)

/**
 * One question in a [UserQuestionPayload]. Empty [options] ⇒ a free-text question; [multiSelect]
 * widens index selection (and requires options server-side). A free-text answer is ALWAYS accepted
 * regardless of [options]/[multiSelect] — the ever-present "Other".
 */
@Serializable
data class UserQuestionSpec(
    val header: String,
    val question: String,
    val options: List<UserQuestionOption> = emptyList(),
    @SerialName("multi_select") val multiSelect: Boolean = false,
)

@Serializable
data class UserQuestionOption(
    val label: String,
    val description: String? = null,
)

/**
 * Wire shape of a `user_question_answered` event (core's `USER_QUESTION_ANSWERED_EVENT`). [outcome]
 * is `answered`|`timed_out`|`declined`|`interrupted`|`cancelled`; [answers]/[notes] are populated
 * only for `answered` (one [AnsweredItem] per question, [AnsweredItem.selectedIndexes] XOR
 * [AnsweredItem.text]). [answeredVia] is the `X-Mewbo-Surface` of whoever answered, so a card can read
 * "answered on console". [delivery] (`"run"` = the answer resolved the still-blocked tool call,
 * `"message"` = the run had already moved on, so the answer landed as a new chat message instead) is
 * only meaningful alongside `answered`. An unknown future [outcome] is tolerated — [TranscriptReducer]
 * treats any non-`answered` value the same as `timed_out`/`declined`/`interrupted`/`cancelled`: the
 * run stopped waiting, but the card stays open for a late answer.
 */
@Serializable
data class UserQuestionAnsweredPayload(
    @SerialName("call_id") val callId: String,
    val outcome: String,
    @SerialName("answered_via") val answeredVia: String? = null,
    val answers: List<AnsweredItem>? = null,
    val notes: String? = null,
    val delivery: String? = null,
)

@Serializable
data class AnsweredItem(
    @SerialName("selected_indexes") val selectedIndexes: List<Int>? = null,
    val text: String? = null,
)
