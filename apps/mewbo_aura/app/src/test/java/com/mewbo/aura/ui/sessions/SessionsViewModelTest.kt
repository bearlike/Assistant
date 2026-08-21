package com.mewbo.aura.ui.sessions

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.RenameSessionRequest
import com.mewbo.aura.data.api.RenameSessionResponseDto
import com.mewbo.aura.data.api.SessionSummaryDto
import com.mewbo.aura.data.api.SessionsListResponseDto
import com.mewbo.aura.data.repo.SessionRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.json.Json
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.mockito.Mockito.mock
import org.mockito.Mockito.`when`

/**
 * The bound `SessionRepository.refreshSessions` puts on the wire. Every stub here must name it, or
 * the mock answers `null` and the fetch NPEs — which is the point: the recents fetch has no
 * unbounded spelling left for a test to accidentally exercise. `SessionRepositoryTest` owns the
 * assertion on the value itself.
 */
private const val RECENTS_FETCH_LIMIT = 50

/**
 * Recents-rail caching contract. The drawer's [SessionsViewModel] is recreated per chat
 * back-stack entry, so it must render the `@Singleton` [SessionRepository]'s last-loaded list
 * INSTANTLY (never the skeleton while a cache exists) and reconcile in the background —
 * stale-while-revalidate — and a failed background refresh must keep that cached list on screen
 * rather than blanking to [SessionsUiState.Error].
 *
 * `viewModelScope` runs on `Dispatchers.Main`; `setMain(StandardTestDispatcher)` shares THIS test's
 * scheduler (via `runTest(dispatcher)`) so `advanceUntilIdle()` drives the `init`/`refresh()` launch.
 * The VM has no infinite `init`-block collector (unlike `AssistTurnMachine`), so the plain
 * MainDispatcher idiom suffices here — the `machineScope()` workaround this project documents for
 * background-collector classes isn't needed.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SessionsViewModelTest {

    private val dispatcher = StandardTestDispatcher()
    private val json = Json { ignoreUnknownKeys = true }

    @Before fun setUp() = Dispatchers.setMain(dispatcher)

    @After fun tearDown() = Dispatchers.resetMain()

    private fun sessionsResponse(vararg ids: String) = SessionsListResponseDto(
        sessions = ids.map { SessionSummaryDto(sessionId = it, title = "Original $it", origin = "mobile") },
    )

    /** A repo whose shared cache is already populated, mirroring a process-alive re-navigation. */
    private suspend fun seededRepo(api: AuraApi, vararg ids: String): SessionRepository {
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenReturn(sessionsResponse(*ids))
        return SessionRepository(api, json).also { it.refreshSessions() }
    }

    @Test
    fun `renders the cached list instantly, then swaps in the background refresh`() = runTest(dispatcher) {
        val api = mock(AuraApi::class.java)
        val repo = seededRepo(api, "s1")
        // The background refresh the freshly-constructed VM fires returns a newer list.
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenReturn(sessionsResponse("s1", "s2"))

        val vm = SessionsViewModel(repo)

        // Before the background fetch runs, the cached row is already on screen — no Loading flash.
        val immediate = vm.uiState.value
        assertTrue("expected instant Loaded, was $immediate", immediate is SessionsUiState.Loaded)
        assertEquals(listOf("s1"), (immediate as SessionsUiState.Loaded).sessions.map { it.sessionId })

        advanceUntilIdle()

        val refreshed = vm.uiState.value as SessionsUiState.Loaded
        assertEquals(listOf("s1", "s2"), refreshed.sessions.map { it.sessionId })
        assertFalse(refreshed.offline)
    }

    @Test
    fun `first launch with an empty cache shows the skeleton, then the fetched list`() = runTest(dispatcher) {
        val api = mock(AuraApi::class.java)
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenReturn(sessionsResponse("s1"))
        val repo = SessionRepository(api, json) // never refreshed — cache genuinely empty

        val vm = SessionsViewModel(repo)

        assertEquals(SessionsUiState.Loading, vm.uiState.value)

        advanceUntilIdle()

        val loaded = vm.uiState.value as SessionsUiState.Loaded
        assertEquals(listOf("s1"), loaded.sessions.map { it.sessionId })
    }

    @Test
    fun `a failed background refresh keeps the cached list rather than blanking`() = runTest(dispatcher) {
        val api = mock(AuraApi::class.java)
        val repo = seededRepo(api, "s1", "s2")
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenThrow(RuntimeException("network down"))

        val vm = SessionsViewModel(repo)

        // Cache rendered immediately, before the failing fetch.
        assertTrue(vm.uiState.value is SessionsUiState.Loaded)

        advanceUntilIdle()

        // The failed refresh degrades to offline — never Error, never an empty list.
        val state = vm.uiState.value
        assertTrue("expected Loaded(offline), was $state", state is SessionsUiState.Loaded)
        state as SessionsUiState.Loaded
        assertTrue(state.offline)
        assertEquals(listOf("s1", "s2"), state.sessions.map { it.sessionId })
    }

    @Test
    fun `renameSession reports success and the rail reflects the new title`() = runTest(dispatcher) {
        val api = mock(AuraApi::class.java)
        val repo = seededRepo(api, "s1")
        `when`(api.renameSession("s1", RenameSessionRequest("New title")))
            .thenReturn(RenameSessionResponseDto(sessionId = "s1", title = "New title"))
        // The trailing server-truth reconciliation sees the renamed row.
        `when`(api.listSessions(false, RECENTS_FETCH_LIMIT)).thenReturn(
            SessionsListResponseDto(
                sessions = listOf(SessionSummaryDto(sessionId = "s1", title = "New title", origin = "mobile")),
            ),
        )

        var reported: Boolean? = null
        val vm = SessionsViewModel(repo)
        vm.renameSession("s1", "New title") { reported = it }
        advanceUntilIdle()

        assertEquals(true, reported)
        val titles = (vm.uiState.value as SessionsUiState.Loaded).sessions.associate { it.sessionId to it.title }
        assertEquals("New title", titles["s1"])
    }
}
