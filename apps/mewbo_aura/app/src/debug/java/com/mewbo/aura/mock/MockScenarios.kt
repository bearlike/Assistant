package com.mewbo.aura.mock

import java.time.Instant
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * ONE place every scripted mock-backend response lives (user directive: "scripted content lives
 * in one place, NOT sprinkled"). Frames are hand-built [JsonObject]s (via kotlinx.serialization's
 * `buildJsonObject` DSL, never raw string interpolation - correct escaping for free) matching the
 * REAL wire shape field-for-field (`data/CLAUDE.md`'s verified contract: bare `{type, ts, payload}`
 * envelopes, `@SerialName` snake_case field names, no synthetic `"error"` event type - failure
 * surfaces exclusively through `completion.error`). [MockScenariosTest] round-trips every frame
 * here through the REAL [com.mewbo.aura.data.model.SessionEvent.decode] - that round-trip, not the
 * scenario content itself, is the actual contract test value (`app/src/test/.../mock/CLAUDE.md`
 * pointer: `app/src/test/java/com/mewbo/aura/CLAUDE.md`).
 */
object MockScenarios {

    /** One scripted turn: `(delayMs, frame)` pairs, [delayMs] the pacing BEFORE that frame is
     * written (mirrors [com.mewbo.aura.voice.AssistTurnMachineTest]'s own `ScriptedTranscriber`
     * shape) - realistic ~80-150ms delta pacing, not instant delivery, so on-device testing still
     * exercises the real streaming/word-fade UI instead of a single frozen frame. The terminal
     * `stream_end` control frame (no `ts`/`payload` on the wire) is appended by
     * [MockBackendInterceptor], not scripted here - it's a transport-layer signal, not a genuine
     * [com.mewbo.aura.data.model.SessionEvent] variant. */
    data class Scenario(val name: String, val frames: List<Pair<Long, JsonObject>>)

    /** Default turn: a short, realistic reply - what every auto-listen overlay invocation gets
     * unless [forQuery] matches a keyword below. */
    val happyPath: Scenario = run {
        val text = "Sure, here's a scripted reply for on-device testing — no live backend was called " +
            "and no real tokens were spent."
        val words = text.split(" ")
        // Chunk into ~3-word deltas so the UI's WordFadeText has multiple deltas to animate, same
        // shape a real token-streaming backend produces - never one giant single-frame dump.
        val chunks = words.chunked(3).map { it.joinToString(" ") + " " }
        buildScenario("happy-path", chunks, finalText = text)
    }

    /** Exercises card scroll/expand + long-generation UI without a fixed-slab-vs-shrink-wrap
     * regression (Gitea #181 item 1) needing a real long backend reply to test against. */
    val longResponse: Scenario = run {
        val sentences = (1..14).map { i ->
            "This is scripted sentence number $i of a long mock reply, used to exercise " +
                "streaming, card scroll, and card-expansion behavior without spending real tokens."
        }
        val finalText = sentences.joinToString(" ")
        val chunks = sentences.map { "$it " }
        buildScenario("long-response", chunks, finalText = finalText)
    }

    /** Exercises the `completion.error` inline-error-card path (`data/CLAUDE.md`: there is no
     * synthetic `"error"` event type - a scripted failure MUST surface through `completion` like a
     * real one does, never a fake dedicated error frame, or this scenario would test a shape the
     * real backend never actually sends). */
    val errorScenario: Scenario = run {
        var t = Instant.now()
        val frames = mutableListOf<Pair<Long, JsonObject>>()
        frames += 100L to deltaFrame(t, "Working on it").also { t = t.plusMillis(100) }
        frames += 150L to completionFrame(t, error = "Mock backend: scripted failure for error-path testing")
        Scenario("error", frames)
    }

    /** Cheap, on-device-discoverable scenario selection purely from the query text - no extra UI
     * needed to reach the long-response/error paths: type or say "trigger a long response" / "give
     * me an error". Defaults to [happyPath] for everything else - the auto-listen overlay's normal
     * scripted turn. */
    fun forQuery(text: String): Scenario = when {
        text.contains("error", ignoreCase = true) -> errorScenario
        text.contains("long", ignoreCase = true) -> longResponse
        else -> happyPath
    }

    private fun buildScenario(name: String, deltaChunks: List<String>, finalText: String): Scenario {
        var t = Instant.now()
        val frames = mutableListOf<Pair<Long, JsonObject>>()
        for (chunk in deltaChunks) {
            val delayMs = DELTA_DELAY_MIN_MS + (chunk.hashCode().mod(DELTA_DELAY_JITTER_MS))
            frames += delayMs to deltaFrame(t, chunk)
            t = t.plusMillis(delayMs)
        }
        frames += ASSISTANT_DELAY_MS to assistantFrame(t, finalText)
        t = t.plusMillis(ASSISTANT_DELAY_MS)
        frames += COMPLETION_DELAY_MS to completionFrame(t)
        return Scenario(name, frames)
    }

    /** Backend timestamps are a numeric offset, never bare `Z` (`app/src/test/.../CLAUDE.md`'s own
     * documented trap) - `Instant.toString()` produces `Z`, so every frame here re-shapes it the
     * same way that file's `AssistTurnMachineTest` fixture does. */
    private fun ts(instant: Instant): String = instant.toString().removeSuffix("Z") + "+00:00"

    private fun deltaFrame(t: Instant, text: String): JsonObject = buildJsonObject {
        put("type", "agent_message_delta")
        put("ts", ts(t))
        putJsonObject("payload") {
            put("text", text)
            put("agent_id", "root")
            put("depth", 0)
        }
    }

    private fun assistantFrame(t: Instant, text: String): JsonObject = buildJsonObject {
        put("type", "assistant")
        put("ts", ts(t))
        putJsonObject("payload") { put("text", text) }
    }

    private fun completionFrame(t: Instant, error: String? = null): JsonObject = buildJsonObject {
        put("type", "completion")
        put("ts", ts(t))
        putJsonObject("payload") {
            put("done", true)
            put("done_reason", if (error != null) "error" else "success")
            if (error != null) put("error", error)
        }
    }

    private const val DELTA_DELAY_MIN_MS = 80L
    private const val DELTA_DELAY_JITTER_MS = 70 // + DELTA_DELAY_MIN_MS => ~80-150ms per delta, per spec
    private const val ASSISTANT_DELAY_MS = 60L
    private const val COMPLETION_DELAY_MS = 40L
}
