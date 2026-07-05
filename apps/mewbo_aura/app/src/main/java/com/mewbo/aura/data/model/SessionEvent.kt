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

    /** A client-executable tool call dispatched to this device (Gitea #179) - resolved by
     * [com.mewbo.aura.data.device.DeviceToolExecutor] posting a result keyed on
     * [DeviceToolCallPayload.callToken]. Never rendered in the chat transcript
     * ([TranscriptReducer] drops it the same as `llm_call_end`/`permission`). */
    @Serializable
    data class DeviceToolCall(override val ts: String, val payload: DeviceToolCallPayload) : SessionEvent

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
         * The MOST RECENT persisted `context` event's payload object, per the backend's own
         * `_load_last_context` semantics (backend.py): the latest context event wins VERBATIM,
         * never merged across several. `context` isn't its own [SessionEvent] variant - the
         * dispatcher has no `"context"` branch (see the class doc's "there is deliberately no error
         * branch" note for the same pattern) - so every context frame decodes to [Unknown] with
         * `type == "context"`; this is the ONE reverse-scan the field readers below share. `null`
         * when the session has no context event at all.
         */
        private fun lastContextPayload(events: List<SessionEvent>): JsonObject? {
            for (event in events.asReversed()) {
                if (event is Unknown && event.type == "context") {
                    return (event.raw as? JsonObject)?.get("payload") as? JsonObject
                }
            }
            return null
        }

        /**
         * The model an EXISTING session's next turn would actually run on: the most recent `context`
         * event's `model` field ([lastContextPayload]). `null` when the session has no context event,
         * or its most recent one has no `model` field (mirrors `context_payload.get("model")` being
         * absent server-side).
         */
        fun lastContextModel(events: List<SessionEvent>): String? =
            lastContextPayload(events)?.get("model")?.jsonPrimitive?.content?.takeIf { it.isNotBlank() }

        /**
         * The project ([ProjectSummary.contextKey] - bare name or `managed:<id>`) an EXISTING
         * session's next turn resolves against: the most recent `context` event's `project` field.
         * Round-trips the value the app itself last sent (backend `_build_context_payload` copies the
         * whole context object verbatim into the persisted event). `null` => the session is scoped to
         * Temporary (no project), which is an honest match of what the backend re-resolves - so a
         * revisited session's frozen scope reflects its REAL project, not the app-wide default.
         */
        fun lastContextProject(events: List<SessionEvent>): String? =
            lastContextPayload(events)?.get("project")?.jsonPrimitive?.content?.takeIf { it.isNotBlank() }

        /**
         * The tool allowlist an EXISTING session's next turn narrows to: the most recent `context`
         * event's `mcp_tools` array. `mcp_tools` is persisted ONLY when the user narrowed the set
         * (data/CLAUDE.md: omitted => the backend binds every tool), so an absent OR empty array
         * round-trips back to `null` (= untouched / all tools), which is exactly the value
         * [ComposerScope.activeToolIds] uses to omit the field again on the next `/query`.
         */
        fun lastContextMcpTools(events: List<SessionEvent>): Set<String>? {
            val raw = lastContextPayload(events)?.get("mcp_tools") as? JsonArray ?: return null
            val ids = raw.mapNotNull { (it as? JsonPrimitive)?.content?.takeIf(String::isNotBlank) }
            return ids.toSet().takeIf { it.isNotEmpty() }
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

/** Wire shape verbatim (Gitea #179 wire contract): `call_token` is consumed exactly once by the
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
