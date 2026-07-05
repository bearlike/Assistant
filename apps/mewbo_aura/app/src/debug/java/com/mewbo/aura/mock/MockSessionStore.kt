package com.mewbo.aura.mock

import java.time.Instant
import java.util.concurrent.ConcurrentHashMap
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.serialization.json.JsonObject

/** One mock-backend session's bookkeeping - enough for [MockBackendInterceptor] to answer
 * `GET /api/sessions` (list) and `GET .../events` (history) consistently with whatever
 * `POST .../query` most recently dispatched, without needing real backend persistence. */
data class MockSessionRecord(
    val sessionId: String,
    val createdAt: String,
    var title: String? = null,
    var scenario: MockScenarios.Scenario? = null,
    var running: Boolean = false,
    /** Provenance the mock reports for `GET /api/sessions`. Aura IS a mobile client, so a session
     * it creates is genuinely `mobile`-origin (matches the real backend's create-time surface tag);
     * [seedDemoIfEmpty] mixes in a couple of `user` records to exercise the rail's scope filter. */
    val origin: String = "mobile",
    /** Mirrors the real backend: `POST .../archive` sets this, and [MockSessionStore.list] hides
     * it from `GET /api/sessions` unless the caller passes `include_archived=true`. */
    var archived: Boolean = false,
)

/** Process-lifetime, in-memory only (no persistence across app restarts - a fresh process gets a
 * fresh empty mock backend, same "just wipe and start over" posture `tools/redroid/CLAUDE.md`'s
 * `data/` wipe has for the container itself). [Singleton] so [MockBackendInterceptor] (constructed
 * once, injected into the app-wide `OkHttpClient`) and any other debug-only caller share ONE table. */
@Singleton
class MockSessionStore @Inject constructor() {
    private val sessions = ConcurrentHashMap<String, MockSessionRecord>()

    fun create(): MockSessionRecord {
        val id = "mock-${System.currentTimeMillis()}-${(0..9999).random()}"
        val record = MockSessionRecord(sessionId = id, createdAt = nowTs())
        sessions[id] = record
        return record
    }

    fun get(sessionId: String): MockSessionRecord? = sessions[sessionId]

    /** [includeArchived] mirrors the real `GET /api/sessions?include_archived=` gate - archived
     * records are hidden from the drawer's default listing but still resolvable individually via
     * [get]/[eventsFor] (matches the real backend: archiving hides, never deletes). */
    fun list(includeArchived: Boolean = false): List<MockSessionRecord> =
        sessions.values.filter { includeArchived || !it.archived }.sortedByDescending { it.createdAt }

    /**
     * Debug-only: populate a realistic Recents list the FIRST time the drawer lists an
     * otherwise-empty mock backend, so the date-grouped rail (Today / Previous 7 days / Older) and
     * the mobile/all scope filter can be exercised on-device without a live backend. The mix of
     * `mobile` and `user` origins proves the default mobile-only scope hides task sessions until the
     * user flips to "All". No-op once any real chat exists (list non-empty), so it never masks a
     * genuinely-created session.
     */
    fun seedDemoIfEmpty() {
        if (sessions.isNotEmpty()) return
        // (title, secondsAgo, origin, running) spanning all three date buckets.
        val demo = listOf(
            DemoSpec("Refactor the auth module", 2L * 3600, "mobile", running = true),
            DemoSpec("Grocery list for the week", 5L * 3600, "mobile", running = false),
            DemoSpec("Trip planning to Kyoto", 2L * 86400, "mobile", running = false),
            DemoSpec("Debug the CI pipeline", 4L * 86400, "user", running = false),
            DemoSpec("Old architecture notes", 20L * 86400, "mobile", running = false),
            DemoSpec("Quarterly planning doc", 45L * 86400, "user", running = false),
        )
        demo.forEachIndexed { index, spec ->
            val id = "mock-demo-$index"
            sessions[id] = MockSessionRecord(
                sessionId = id,
                createdAt = tsAgo(spec.secondsAgo),
                title = spec.title,
                running = spec.running,
                origin = spec.origin,
            )
        }
    }

    private data class DemoSpec(val title: String, val secondsAgo: Long, val origin: String, val running: Boolean)

    private fun tsAgo(seconds: Long): String =
        Instant.now().minusSeconds(seconds).toString().removeSuffix("Z") + "+00:00"

    /** [POST .../query]'s effect: pick a scenario for [queryText], mark the session running, and
     * remember the FIRST query's text as the session's title (matches the real backend's own
     * first-query-becomes-title convention closely enough for list-screen legibility). */
    fun startTurn(sessionId: String, queryText: String): MockScenarios.Scenario {
        val record = sessions.getOrPut(sessionId) { MockSessionRecord(sessionId = sessionId, createdAt = nowTs()) }
        val scenario = MockScenarios.forQuery(queryText)
        record.scenario = scenario
        record.running = true
        if (record.title == null) record.title = queryText.take(TITLE_MAX_CHARS)
        return scenario
    }

    fun finishTurn(sessionId: String) {
        sessions[sessionId]?.running = false
    }

    /** `PATCH .../title`'s effect. No-op (not a lookup failure) if [sessionId] is unknown - the
     * caller ([MockBackendInterceptor.handleRename]) already 404s before reaching here. */
    fun rename(sessionId: String, title: String) {
        sessions[sessionId]?.title = title
    }

    /** `POST .../archive`'s effect - see [MockSessionRecord.archived] for the hide-not-delete
     * semantics this flips on. */
    fun archive(sessionId: String) {
        sessions[sessionId]?.archived = true
    }

    /** `GET .../events` backfill - the full scripted turn's frames (minus pacing, all at once),
     * matching the real backend's own "full persisted backlog on every read" contract
     * (`data/CLAUDE.md`: "Reconnect replays the FULL backlog"). Empty for a session with no
     * dispatched turn yet (freshly created, nothing sent). */
    fun eventsFor(sessionId: String): List<JsonObject> = sessions[sessionId]?.scenario?.frames?.map { it.second } ?: emptyList()

    private fun nowTs(): String = Instant.now().toString().removeSuffix("Z") + "+00:00"

    private companion object {
        const val TITLE_MAX_CHARS = 80
    }
}
