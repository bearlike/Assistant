package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.ArchiveSessionResponseDto
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.ForkSessionRequest
import com.mewbo.aura.data.api.ForkSessionResponseDto
import com.mewbo.aura.data.api.RenameSessionRequest
import com.mewbo.aura.data.api.RenameSessionResponseDto
import com.mewbo.aura.data.api.SessionSummaryDto
import com.mewbo.aura.data.api.SessionsListResponseDto
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mockito.Mockito.mock
import org.mockito.Mockito.verify
import org.mockito.Mockito.`when`
import retrofit2.Response

/**
 * The bound [SessionRepository.refreshSessions] is expected to put on the wire, spelled as a LITERAL
 * rather than imported from production. The production constant is file-private, but that is not the
 * reason: asserting a constant against itself proves nothing, whereas a literal makes changing the
 * bound fail here and forces whoever changes it to re-justify the number against the measurement in
 * its KDoc.
 */
private const val RECENTS_FETCH_LIMIT = 50

/**
 * Drawer long-press sheet (Rename/Archive): [SessionRepository.renameSession] and
 * [SessionRepository.archiveSession] must update the cached [SessionRepository.sessions]
 * `StateFlow` in place on success - the drawer redraws instantly, before any [SessionRepository.refreshSessions]
 * re-fetch - and leave the cache untouched on failure (same degrade idiom as [ModelRepository.catalog]).
 */
class SessionRepositoryTest {

    private val json = Json { ignoreUnknownKeys = true }

    private suspend fun repoWithSessions(api: AuraApi, vararg ids: String): SessionRepository {
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenReturn(
            SessionsListResponseDto(sessions = ids.map { SessionSummaryDto(sessionId = it, title = "Original $it") }),
        )
        val repo = SessionRepository(api, json)
        repo.refreshSessions()
        return repo
    }

    /**
     * The drawer refreshes on EVERY open, so an unbounded read here is paid per gesture and grows
     * with the store forever — measured at 721 rows / 3.07 MB / 1.77 s before this bound existed.
     * The wire call is where that is decided, so the wire call is what this asserts.
     */
    @Test
    fun `refreshSessions sends a bounded limit rather than reading the whole store`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenReturn(
            SessionsListResponseDto(sessions = listOf(SessionSummaryDto(sessionId = "s1"))),
        )

        val repo = SessionRepository(api, json)
        repo.refreshSessions()

        verify(api).listSessions(false, RECENTS_FETCH_LIMIT)
    }

    /** The bound rides ALONGSIDE the existing filter — the server pages what the filter admitted,
     * so a bound that dropped `include_archived` would page a different candidate set. */
    @Test
    fun `refreshSessions carries include_archived through with the bound`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.listSessions(true, RECENTS_FETCH_LIMIT)).thenReturn(
            SessionsListResponseDto(sessions = listOf(SessionSummaryDto(sessionId = "s1"))),
        )

        val repo = SessionRepository(api, json)
        repo.refreshSessions(includeArchived = true)

        verify(api).listSessions(true, RECENTS_FETCH_LIMIT)
    }

    @Test
    fun `renameSession success updates the cached title in place`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1", "s2")
        `when`(api.renameSession("s1", RenameSessionRequest("New title")))
            .thenReturn(RenameSessionResponseDto(sessionId = "s1", title = "New title"))

        val result = repo.renameSession("s1", "New title")

        assertTrue(result)
        val titles = repo.sessions.value.associate { it.sessionId to it.title }
        assertEquals("New title", titles["s1"])
        assertEquals("Original s2", titles["s2"])
    }

    @Test
    fun `renameSession failure returns false and leaves the cache untouched`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.renameSession("s1", RenameSessionRequest("New title"))).thenThrow(RuntimeException("404 unknown session"))

        val result = repo.renameSession("s1", "New title")

        assertFalse(result)
        assertEquals("Original s1", repo.sessions.value.single().title)
    }

    @Test
    fun `renameSession propagates cancellation rather than degrading to false`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.renameSession("s1", RenameSessionRequest("New title"))).thenThrow(CancellationException("cancelled"))

        try {
            repo.renameSession("s1", "New title")
            throw AssertionError("expected CancellationException to propagate")
        } catch (e: CancellationException) {
            // expected - see ModelRepository.catalog's own fix-wave discipline this mirrors.
        }
    }

    @Test
    fun `archiveSession success removes the row from the cache`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1", "s2")
        `when`(api.archiveSession("s1")).thenReturn(ArchiveSessionResponseDto(sessionId = "s1", archived = true))

        val result = repo.archiveSession("s1")

        assertTrue(result)
        assertEquals(listOf("s2"), repo.sessions.value.map { it.sessionId })
    }

    @Test
    fun `archiveSession failure returns false and leaves the cache untouched`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.archiveSession("s1")).thenThrow(RuntimeException("404 unknown session"))

        val result = repo.archiveSession("s1")

        assertFalse(result)
        assertEquals(listOf("s1"), repo.sessions.value.map { it.sessionId })
    }

    @Test
    fun `archiveSession propagates cancellation rather than degrading to false`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.archiveSession("s1")).thenThrow(CancellationException("cancelled"))

        try {
            repo.archiveSession("s1")
            throw AssertionError("expected CancellationException to propagate")
        } catch (e: CancellationException) {
            // expected - see ModelRepository.catalog's own fix-wave discipline this mirrors.
        }
    }

    @Test
    fun `forkSession returns the new id on 201 and leaves the source session untouched in the cache`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.forkSession("s1", ForkSessionRequest())).thenReturn(
            Response.success(201, ForkSessionResponseDto(sessionId = "s1-fork", forkedFrom = "s1")),
        )
        // forkSession's own best-effort refreshSessions() re-fetches the list - simulate the
        // backend now reporting both the source and the new forked row.
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenReturn(
            SessionsListResponseDto(
                sessions = listOf(
                    SessionSummaryDto(sessionId = "s1", title = "Original s1"),
                    SessionSummaryDto(sessionId = "s1-fork", title = "Original s1"),
                ),
            ),
        )

        val newId = repo.forkSession("s1")

        assertEquals("s1-fork", newId)
        val titles = repo.sessions.value.associate { it.sessionId to it.title }
        assertEquals("Original s1", titles["s1"])
        assertEquals("Original s1", titles["s1-fork"])
    }

    @Test
    fun `forkSession returns the new id even when the best-effort refreshSessions afterwards throws`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.forkSession("s1", ForkSessionRequest())).thenReturn(
            Response.success(201, ForkSessionResponseDto(sessionId = "s1-fork", forkedFrom = "s1")),
        )
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenThrow(RuntimeException("network down"))

        val newId = repo.forkSession("s1")

        assertEquals("s1-fork", newId)
    }

    @Test
    fun `forkSession returns null on an error response and leaves the cache untouched`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.forkSession("s1", ForkSessionRequest())).thenReturn(
            Response.error(409, "conflict".toResponseBody("application/json".toMediaType())),
        )

        val newId = repo.forkSession("s1")

        assertNull(newId)
        assertEquals(listOf("s1"), repo.sessions.value.map { it.sessionId })
    }

    @Test
    fun `forkSession returns null on transport failure without touching the cache`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.forkSession("s1", ForkSessionRequest())).thenThrow(RuntimeException("network down"))

        val newId = repo.forkSession("s1")

        assertNull(newId)
        assertEquals(listOf("s1"), repo.sessions.value.map { it.sessionId })
    }

    @Test
    fun `forkSession propagates cancellation rather than degrading to null`() = runTest {
        val api = mock(AuraApi::class.java)
        val repo = repoWithSessions(api, "s1")
        `when`(api.forkSession("s1", ForkSessionRequest())).thenThrow(CancellationException("cancelled"))

        try {
            repo.forkSession("s1")
            throw AssertionError("expected CancellationException to propagate")
        } catch (e: CancellationException) {
            // expected - see ModelRepository.catalog's own fix-wave discipline this mirrors.
        }
    }
}
