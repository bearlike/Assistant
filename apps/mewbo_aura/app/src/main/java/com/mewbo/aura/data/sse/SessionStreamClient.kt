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

        while (isActive && !terminated) {
            try {
                connectOnce(sessionId) { event ->
                    trySend(event)
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

    private suspend fun connectOnce(sessionId: String, onEvent: (SessionEvent) -> Unit) {
        val baseUrl = settingsStore.baseUrl.first().trimEnd('/')
        val apiKey = settingsStore.apiKey.first().orEmpty()
        val url = "$baseUrl/api/sessions/$sessionId/stream".toHttpUrlOrNull()
            ?.newBuilder()
            ?.addQueryParameter("api_key", apiKey)
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
