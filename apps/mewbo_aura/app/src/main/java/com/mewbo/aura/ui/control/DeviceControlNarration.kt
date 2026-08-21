package com.mewbo.aura.ui.control

import androidx.compose.runtime.Immutable
import com.mewbo.aura.data.model.DeviceToolCallPayload
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.ui.theme.AuraMotion
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * One transient line of narration drawn over whatever app the agent is driving.
 *
 * [source] is the FULL text the line was built from, kept rather than discarded because a
 * streaming reply arrives in fragments: appending a delta to an already-shortened line would
 * append to an ellipsis. [text] is the one line actually drawn, derived once at construction.
 *
 * [shownAtMs] is a caller-supplied monotonic reading, never a clock this class reads — the whole
 * fold is exercised on a plain JVM with fixed times instead of sleeps.
 */
@Immutable
data class ControlBubble(
    /** Stable across updates, so a streaming line refreshes IN PLACE instead of stacking a new
     * line per delta. `agent:<id>` for the model's narration, `call:<id>` for a tool call. */
    val id: String,
    val source: String,
    val shownAtMs: Long,
) {
    /** The single short line drawn. Computed at construction, so it is not re-derived on every
     * recomposition of a surface that redraws at frame rate. */
    val text: String = tail(source)

    internal companion object {
        /** Roughly one line at `chipLabel` across a phone's width, minus the pill's own padding.
         * A glance surface, deliberately not a transcript — the chat surface is where the whole
         * reply lives. */
        const val MAX_CHARS = 72

        private val WHITESPACE = Regex("\\s+")

        /**
         * The LAST [MAX_CHARS] of [source], opened at a word boundary.
         *
         * The tail rather than the head, and that is the whole point: while a reply streams, a
         * head-truncated line stops changing the moment the text passes the cap, so the one
         * surface whose entire job is to show that something is happening would freeze. Newlines
         * collapse to spaces — this is a single line, and a markdown list arriving mid-reply must
         * not turn it into three.
         */
        fun tail(source: String): String {
            val flat = source.trim().replace(WHITESPACE, " ")
            if (flat.length <= MAX_CHARS) return flat
            val cut = flat.takeLast(MAX_CHARS - 1)
            // Open at a word boundary so the line never starts mid-word. A tail carrying no space
            // at all (one very long token, e.g. a URL) keeps the raw cut rather than emptying out.
            val aligned = cut.substringAfter(' ', cut).trimStart()
            return "…$aligned"
        }
    }
}

/**
 * What the overlay is currently saying: the fold from session events to a short, self-expiring
 * stack of lines.
 *
 * Pure and total. Every input is either a typed [SessionEvent] or a monotonic `nowMs` the caller
 * supplies, so the ordering, the replacement rule and the expiry are all testable without a
 * device, a clock or a coroutine. The window controller owns the I/O; this owns the decision.
 *
 * Only three event types narrate. Everything else returns THIS INSTANCE — identity, not an equal
 * copy — so a `MutableStateFlow.update` over a busy stream emits nothing and the overlay does not
 * recompose on traffic it does not draw.
 */
@Immutable
data class DeviceControlNarration(val bubbles: List<ControlBubble> = emptyList()) {

    /**
     * Fold one event in.
     *
     * `agent_message` REPLACES the accumulated delta text for that agent while
     * `agent_message_delta` APPENDS to it — the wire contract's own distinction
     * ([SessionEvent.AgentMessage] is authoritative for the in-progress step), not a rule
     * invented here. Both key on the agent id, so a sub-agent narrates on its own line instead of
     * interleaving into the root agent's sentence.
     */
    fun fold(event: SessionEvent, nowMs: Long): DeviceControlNarration = when (event) {
        is SessionEvent.AgentMessage ->
            stream(event.payload.agentId, event.payload.text, append = false, nowMs = nowMs)

        is SessionEvent.AgentMessageDelta ->
            stream(event.payload.agentId, event.payload.text, append = true, nowMs = nowMs)

        is SessionEvent.DeviceToolCall -> add(
            ControlBubble(
                id = "call:${event.payload.callId}",
                source = label(event.payload),
                shownAtMs = nowMs,
            ),
        )

        else -> this
    }

    /** Drop whatever has outlived [AuraMotion.transientDismissMs] — the app's ONE sense of how
     * long a transient thing lingers, shared with the one-line notice. Returns THIS INSTANCE when
     * nothing expired, for the same no-recomposition reason [fold] does. */
    fun expire(nowMs: Long): DeviceControlNarration {
        val live = bubbles.filter { nowMs - it.shownAtMs < AuraMotion.transientDismissMs }
        return if (live.size == bubbles.size) this else copy(bubbles = live)
    }

    /**
     * A streaming line, refreshed in place.
     *
     * Refreshed IN PLACE rather than moved to the end: the model narrates continuously while tool
     * calls land between its sentences, and re-ordering the stack under the reader every few
     * hundred milliseconds is unreadable on a surface meant for a glance. The refreshed
     * [ControlBubble.shownAtMs] is what keeps a line that is still being written from expiring
     * underneath itself.
     */
    private fun stream(
        agentId: String,
        text: String,
        append: Boolean,
        nowMs: Long,
    ): DeviceControlNarration {
        val id = "agent:$agentId"
        val existing = bubbles.firstOrNull { it.id == id }
        val source = if (append) (existing?.source ?: "") + text else text
        // A blank authoritative message is a real wire value (a turn that only called tools); it
        // must not blank an already-drawn line or add an empty pill.
        if (source.isBlank()) return this
        val updated = ControlBubble(id = id, source = source, shownAtMs = nowMs)
        return if (existing == null) {
            add(updated)
        } else {
            copy(bubbles = bubbles.map { if (it.id == id) updated else it })
        }
    }

    private fun add(bubble: ControlBubble): DeviceControlNarration =
        copy(bubbles = (bubbles + bubble).takeLast(MAX_VISIBLE))

    companion object {
        /** Three lines is the most this can carry without becoming a transcript on top of an app
         * the user is trying to see. The oldest falls off. */
        const val MAX_VISIBLE = 3

        /**
         * What a device tool call is DOING, in the words of whoever is holding the phone.
         *
         * The vocabulary is `DeviceToolCatalog`'s own (`device_ui` elements|screenshot;
         * `device_action` tap|swipe|type|key|launch|wait; `device_shell`), and every unknown arm
         * falls back to the tool id rather than to silence — a tool this file has not heard of
         * must still say that something happened.
         *
         * **Args are model output, so the parse is total.** `as? JsonPrimitive`, never the
         * `jsonPrimitive` accessor, which THROWS on a nested object or array — and this runs on
         * the live event path, where a throw would take the collector down with it.
         */
        internal fun label(payload: DeviceToolCallPayload): String {
            val args = payload.args
            return when (payload.toolId) {
                "device_ui" -> when (args.arg("action")) {
                    "screenshot" -> "Looking at the screen"
                    else -> "Reading the screen"
                }

                "device_action" -> when (args.arg("action")) {
                    "tap" -> "Tapping"
                    "type" -> "Typing"
                    "swipe" -> args.arg("direction")?.let { "Swiping $it" } ?: "Swiping"
                    "key" -> args.arg("key")?.let { "Pressing $it" } ?: "Pressing a key"
                    "launch" -> args.arg("package_name")?.let { "Opening $it" } ?: "Opening an app"
                    "wait" -> "Waiting for the screen"
                    else -> "Acting on the screen"
                }

                "device_shell" -> "Running a command"
                else -> payload.toolId.removePrefix("device_").replace('_', ' ')
            }
        }

        private fun JsonObject.arg(key: String): String? =
            (this[key] as? JsonPrimitive)?.content?.takeIf { it.isNotBlank() }
    }
}
