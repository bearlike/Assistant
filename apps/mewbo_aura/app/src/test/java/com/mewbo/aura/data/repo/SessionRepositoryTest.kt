package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.ArchiveSessionResponseDto
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.RenameSessionRequest
import com.mewbo.aura.data.api.RenameSessionResponseDto
import com.mewbo.aura.data.api.SessionSummaryDto
import com.mewbo.aura.data.api.SessionsListResponseDto
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mockito.Mockito.mock
import org.mockito.Mockito.`when`

/**
 * Drawer long-press sheet (Rename/Archive): [SessionRepository.renameSession] and
 * [SessionRepository.archiveSession] must update the cached [SessionRepository.sessions]
 * `StateFlow` in place on success - the drawer redraws instantly, before any [SessionRepository.refreshSessions]
 * re-fetch - and leave the cache untouched on failure (same degrade idiom as [ModelRepository.catalog]).
 */
class SessionRepositoryTest {

    private val json = Json { ignoreUnknownKeys = true }

    private suspend fun repoWithSessions(api: AuraApi, vararg ids: String): SessionRepository {
        `when`(api.listSessions(false)).thenReturn(
            SessionsListResponseDto(sessions = ids.map { SessionSummaryDto(sessionId = it, title = "Original $it") }),
        )
        val repo = SessionRepository(api, json)
        repo.refreshSessions()
        return repo
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
}
