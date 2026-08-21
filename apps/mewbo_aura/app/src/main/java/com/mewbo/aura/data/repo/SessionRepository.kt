package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.ForkSessionRequest
import com.mewbo.aura.data.api.RenameSessionRequest
import com.mewbo.aura.data.api.SessionCreateRequest
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.SessionHistory
import com.mewbo.aura.data.model.SessionSummary
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.serialization.json.Json

/**
 * How many candidates one recents fetch lets the server examine — measured, not chosen by taste.
 *
 * The drawer narrows client-side to mobile-origin rows by default
 * ([com.mewbo.aura.ui.sessions.RecentsFilter.MOBILE_ONLY]), so the bound has to be deep enough that
 * the FILTERED list still fills the rail. Against the deployed store (721 sessions), newest-first:
 * 50 candidates returned 46 rows of which 24 were mobile, and 100 candidates returned 84 rows
 * carrying the SAME 24 mobile rows — mobile yield SATURATES at 50, so doubling the bound buys the
 * rail zero extra rows for +434 KB. 24 rows is roughly twice a drawer screenful, and the transfer
 * drops from 3,071,030 bytes / 1.77 s to 413,708 bytes / 0.17 s.
 *
 * This is a ceiling on the WORK, not on the response alone: the server applies it at the store.
 */
private const val RECENTS_FETCH_LIMIT = 50

/**
 * List/create/history for sessions. Backend is the source of truth (D-5, no local database) - this
 * only keeps a simple in-memory cache of the last-fetched list and per-session transcripts so
 * screens have something to render immediately on process-alive navigation.
 *
 * **`@Singleton`.** The `render-immediately-on-navigation` intent above only holds if the
 * cache is SHARED across injectors: the drawer's [com.mewbo.aura.ui.sessions.SessionsViewModel] is
 * recreated per chat back-stack entry, so without a shared instance every drawer-open-after-navigation
 * started from an empty [sessions] cache and blanked to the skeleton for a full `GET /sessions`
 * round-trip. One instance (mirroring [RunRepository]'s own `@Singleton`) means the last-loaded list
 * survives that recreation and a mutation (rename/archive) made through one injector is visible to all.
 * The only state is the two in-memory caches ([sessions]/[transcripts]) - no per-injector state.
 */
@Singleton
class SessionRepository @Inject constructor(
    private val api: AuraApi,
    private val json: Json,
) {
    private val _sessions = MutableStateFlow<List<SessionSummary>>(emptyList())
    val sessions: StateFlow<List<SessionSummary>> = _sessions.asStateFlow()

    private val _transcripts = MutableStateFlow<Map<String, SessionHistory>>(emptyMap())
    val transcripts: StateFlow<Map<String, SessionHistory>> = _transcripts.asStateFlow()

    /**
     * Fetches the recents list. **Cost: `O(collection)`, capped at [RECENTS_FETCH_LIMIT]
     * candidates** — never `O(all history)`, which is what this was before the cap
     * ([AuraApi.listSessions] carries the measurement).
     *
     * The bound is NOT a parameter, deliberately. Every caller here wants the recents window (the
     * drawer's refresh-on-open, the assist machine's continue-last-session lookup, `forkSession`'s
     * best-effort re-read), and a `limit` parameter defaulted to "everything" is exactly the
     * fail-open the required parameter one layer down exists to prevent. A future caller that
     * genuinely needs a different window adds its own method rather than widening this one.
     */
    suspend fun refreshSessions(includeArchived: Boolean = false): List<SessionSummary> {
        val response = api.listSessions(includeArchived, RECENTS_FETCH_LIMIT)
        val summaries = response.sessions.map { it.toDomain() }
        _sessions.value = summaries
        return summaries
    }

    /**
     * Creates the session shell only - no run starts until [com.mewbo.aura.data.repo.RunRepository.sendQuery]
     * (fresh turn) or [com.mewbo.aura.data.repo.RunRepository.send] (steer). [model]/[project]/
     * [mcpTools] seed `context` the same way every subsequent `/query` call does
     * ([buildSessionContext]) - the backend re-resolves them per-request, not just at creation, so
     * [com.mewbo.aura.ui.chat.ChatViewModel] resends the SAME values on every later send too.
     */
    suspend fun createSession(
        sessionTag: String? = null,
        mode: String? = null,
        model: String? = null,
        project: String? = null,
        mcpTools: List<String>? = null,
    ): String {
        val response = api.createSession(
            SessionCreateRequest(
                sessionTag = sessionTag,
                mode = mode,
                context = buildSessionContext(model = model, project = project, mcpTools = mcpTools),
            ),
        )
        return response.sessionId
    }

    /**
     * Forks a NEW session from [sessionId], copying its transcript - the source session is NEVER
     * modified. A null [fromTs] copies the WHOLE transcript; a set [fromTs] copies up to and
     * including that message. Returns the new session id, or null on failure (409 running / 410
     * terminated / transport) - same degrade-to-null idiom as [renameSession]/[archiveSession].
     * On success also refreshes the drawer's cached [sessions] list so the fork shows up there;
     * that refresh is best-effort and isolated - its own failure must not lose the already-forked id.
     */
    suspend fun forkSession(sessionId: String, fromTs: String? = null, model: String? = null): String? {
        val response = runCatching { api.forkSession(sessionId, ForkSessionRequest(fromTs = fromTs, model = model)) }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()
        val newSessionId = response?.takeIf { it.isSuccessful }?.body()?.sessionId ?: return null
        runCatching { refreshSessions() }.onFailure { if (it is CancellationException) throw it }
        return newSessionId
    }

    suspend fun fetchHistory(sessionId: String, after: String? = null): SessionHistory {
        val response = api.getEvents(sessionId, after)
        val events = response.events.map { SessionEvent.decode(json, it) }
        val history = SessionHistory(
            sessionId = response.sessionId,
            events = events,
            running = response.running,
            status = response.status,
            doneReason = response.doneReason,
            title = response.title,
            recoverable = response.recoverable,
            terminated = response.terminated,
            terminatedAt = response.terminatedAt,
        )
        _transcripts.update { it + (sessionId to history) }
        return history
    }

    fun cachedHistory(sessionId: String): SessionHistory? = _transcripts.value[sessionId]

    /**
     * `true` on HTTP success, updating the cached drawer row's title in place so it reflects
     * instantly without waiting for the next [refreshSessions]. Same degrade-on-failure idiom as
     * [ModelRepository.catalog] - rethrow a [CancellationException], otherwise fail to `false`.
     */
    suspend fun renameSession(sessionId: String, title: String): Boolean {
        val response = runCatching { api.renameSession(sessionId, RenameSessionRequest(title)) }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull() ?: return false
        _sessions.update { list ->
            list.map { if (it.sessionId == sessionId) it.copy(title = response.title ?: title) else it }
        }
        return true
    }

    /**
     * Pin ([pinned] true) or unpin the session. `true` on HTTP success, flipping the cached drawer
     * row in place so the rail re-sections instantly — same degrade-to-`false` idiom as
     * [renameSession]/[archiveSession].
     *
     * The REQUESTED state is what gets written to the cache, not a state read back off the
     * response: the acknowledgement's shape is deliberately over-tolerant
     * ([com.mewbo.aura.data.api.PinSessionResponseDto]), so a body that omitted `pinned` would
     * otherwise silently un-pin the row the server just pinned. `pinnedAt` is adopted only when the
     * server actually sent one — the client never mints a timestamp, and the next
     * [refreshSessions] carries server truth regardless.
     */
    suspend fun setPinned(sessionId: String, pinned: Boolean): Boolean {
        val response = runCatching {
            if (pinned) api.pinSession(sessionId) else api.unpinSession(sessionId)
        }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull() ?: return false
        _sessions.update { list ->
            list.map {
                if (it.sessionId == sessionId) {
                    it.copy(pinned = pinned, pinnedAt = if (pinned) response.pinnedAt ?: it.pinnedAt else null)
                } else {
                    it
                }
            }
        }
        return true
    }

    /** `true` on HTTP success, removing the row from the cached drawer list in place. */
    suspend fun archiveSession(sessionId: String): Boolean {
        val succeeded = runCatching { api.archiveSession(sessionId) }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull() != null
        if (succeeded) {
            _sessions.update { list -> list.filterNot { it.sessionId == sessionId } }
        }
        return succeeded
    }
}
