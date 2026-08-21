package com.mewbo.aura.data.sse

import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.settings.SettingsStore
import java.io.IOException
import javax.inject.Inject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.isActive
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Request
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener

/**
 * Owns ALL SSE mechanics for `GET /api/sessions/{id}/stream` (bare `data: <json>` frames, no
 * `id:`/`event:` lines - api-contract.md section 3). UI/repo layers only ever see
 * `Flow<SessionEvent>`, never OkHttp.
 *
 * Reconnect design note (deliberate divergence from the v1 spec's naive "call /events?after= then
 * resubscribe" wording): the verified contract states the server has NO partial-resume capability
 * on this endpoint - every (re)connect replays the FULL persisted backlog, unlike `/events?after=`.
 * Doing an extra REST backfill call before resubscribing would therefore just be redundant network
 * traffic that gets re-sent again in full over the reopened SSE connection anyway.
 *
 * This class deliberately does NOT filter or dedupe by `ts` - a strictly-greater-than cursor is
 * lossy (two distinct events can legitimately share an identical timestamp, api-contract.md section
 * 4) and would sit upstream of, and defeat, [com.mewbo.aura.data.model.TranscriptReducer]'s
 * content-key dedupe. Every parsed event is emitted, on every (re)connection, including exact
 * repeats from a full backlog replay. That's safe by design: [TranscriptReducer.reduce] is pure and
 * idempotent on the full event list, so re-delivering the backlog produces identical output, not
 * duplicates. Dedupe belongs to, and only to, the reducer.
 */
class SessionStreamClient @Inject constructor(
    private val eventSourceFactory: EventSource.Factory,
    private val json: Json,
    private val settingsStore: SettingsStore,
) {
    fun stream(sessionId: String): Flow<SessionEvent> = callbackFlow {
        var backoffMs = INITIAL_BACKOFF_MS
        var terminated = false
        // Newest `ts` delivered so far, replayed to the server as `?after=` on every RECONNECT (never
        // on the first connect, which wants the whole backlog). See [connectOnce] for why an
        // inclusive cursor is safe here when a client-side one would not be.
        var afterTs: String? = null

        while (isActive && !terminated) {
            try {
                connectOnce(sessionId, afterTs) { event ->
                    trySend(event)
                    // A `device_tool_call` deliberately does NOT advance the cursor, and this is the
                    // one place the trimming can lose work rather than merely repeat it. Receiving
                    // that event is not the same as ANSWERING it: dispatch is asynchronous, so a
                    // connection dying between the two leaves a call the model is still waiting on.
                    // Full replay used to be what recovered it; a cursor past the call removes that
                    // net and the model waits out its timeout instead. Holding the cursor at the
                    // newest call means every reconnect re-delivers it, and `DeviceToolCallLedger`
                    // makes the repeat a no-op — the same idempotence full replay always relied on.
                    if (event.ts.isNotBlank() && event !is SessionEvent.DeviceToolCall) afterTs = event.ts
                    if (event is SessionEvent.StreamEnd) terminated = true
                }
                backoffMs = INITIAL_BACKOFF_MS
            } catch (e: CancellationException) {
                throw e
            } catch (e: IOException) {
                // Connection dropped mid-run - fall through to backoff + reconnect below.
            }
            if (terminated || !isActive) break
            kotlinx.coroutines.delay(backoffMs)
            backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
        }
        close()
        awaitClose { }
    }

    /**
     * One SSE connection. [afterTs] trims the once-only backlog replay to that timestamp or later;
     * `null` (the first connect) asks for the whole backlog.
     *
     * **The server's `after` is INCLUSIVE, and that is exactly why using it does not contradict this
     * class's no-client-side-cursor rule.** The rejected design was a client-side strictly-greater
     * filter, which silently DROPS an event sharing a timestamp with the last one seen. Asking the
     * server for "this timestamp or later" cannot lose an event: everything sharing the cursor's `ts`
     * is re-sent, and the duplicates that creates are handled where they always were — by
     * `TranscriptReducer`'s content-key dedupe. Nothing is filtered here; the cursor narrows the
     * server's WORK, not the client's view of it.
     */
    private suspend fun connectOnce(sessionId: String, afterTs: String?, onEvent: (SessionEvent) -> Unit) {
        val baseUrl = settingsStore.baseUrl.first().trimEnd('/')
        val apiKey = settingsStore.apiKey.first().orEmpty()
        val url = "$baseUrl/api/sessions/$sessionId/stream".toHttpUrlOrNull()
            ?.newBuilder()
            ?.addQueryParameter("api_key", apiKey)
            ?.apply { if (!afterTs.isNullOrBlank()) addQueryParameter("after", afterTs) }
            ?.build()
            ?: throw IOException("Invalid base URL: $baseUrl")

        suspendCancellableCoroutine<Unit> { cont ->
            val request = Request.Builder().url(url).build()
            val listener = object : EventSourceListener() {
                override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                    onEvent(SessionEvent.decode(json, data))
                }

                override fun onClosed(eventSource: EventSource) {
                    if (cont.isActive) cont.resumeWith(Result.success(Unit))
                }

                override fun onFailure(eventSource: EventSource, t: Throwable?, response: okhttp3.Response?) {
                    if (cont.isActive) {
                        cont.resumeWith(Result.failure(t ?: IOException("SSE connection failed")))
                    }
                }
            }
            val source = eventSourceFactory.newEventSource(request, listener)
            cont.invokeOnCancellation { source.cancel() }
        }
    }

    private companion object {
        const val INITIAL_BACKOFF_MS = 500L
        const val MAX_BACKOFF_MS = 15_000L
    }
}
