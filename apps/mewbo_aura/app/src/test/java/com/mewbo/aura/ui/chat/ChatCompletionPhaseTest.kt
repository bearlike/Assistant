package com.mewbo.aura.ui.chat

import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.CompletionPayload
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TranscriptReducer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Contract for [completionPhaseFor] — the run-outcome decision the chat surface makes when a
 * `completion` event lands. Deliberately the MIRROR of `RunNotificationControllerTest`'s
 * `completionNotice` suite: the two surfaces answer the same question about the same payload
 * (the composer's phase here, the notification there), so pinning them with symmetric tests is what
 * stops one of them drifting again.
 *
 * A numeric-offset `ts` matches the backend's real `isoformat()` shape — irrelevant to this
 * decision, but the house rule is to never seed a fixture from the bare-`Z` form (`test/CLAUDE.md`).
 */
class ChatCompletionPhaseTest {

    private val ts = "2026-07-14T00:00:00.000000+00:00"

    private fun completion(error: String? = null, lastError: String? = null) =
        SessionEvent.Completion(ts, CompletionPayload(error = error, lastError = lastError))

    @Test
    fun `a clean completion settles the run into Done`() {
        assertEquals(RunPhase.Done, completionPhaseFor(completion()))
    }

    @Test
    fun `a genuine run failure settles the run into Error`() {
        assertEquals(RunPhase.Error, completionPhaseFor(completion(error = "boom")))
    }

    @Test
    fun `last_error residue on a successful run is Done, never Error`() {
        // The user-reported bug: an MCP/tool-call error INSIDE a fully successful session surfaced
        // as a session-level failure. `last_error` is a sticky diagnostic of the last tool failure;
        // the orchestrator sets `error` only on its own terminal-failure path, so a run that
        // recovered and went on to answer is a success (DESIGN.md §6 error-card law).
        assertEquals(RunPhase.Done, completionPhaseFor(completion(error = null, lastError = "one tool failed")))
    }

    @Test
    fun `an empty error string is still a failure`() {
        // The reducer treats non-null-but-empty as a failure too (it falls back to `lastError` for
        // the card's message rather than dropping the card) — so the phase must not disagree.
        assertEquals(RunPhase.Error, completionPhaseFor(completion(error = "")))
    }

    @Test
    fun `every non-completion event leaves the phase untouched`() {
        // `null` means "don't write runPhase" — ChatViewModel.applyEvent passes it straight through.
        assertNull(completionPhaseFor(SessionEvent.StreamEnd))
        assertNull(completionPhaseFor(SessionEvent.StreamError(message = "socket closed")))
    }

    // ---- the phase and the card must agree, proven against the REAL reducer ----

    private fun cards(event: SessionEvent.Completion): List<ChatItem.ErrorCard> =
        TranscriptReducer.fold(TranscriptReducer.State(), event).chatItems.filterIsInstance<ChatItem.ErrorCard>()

    @Test
    fun `last_error residue spawns neither an Error phase nor an ErrorCard`() {
        // The two halves of one decision, asserted together: a phase without a card (or a card
        // without a phase) is the chat surface disagreeing with itself about whether the turn worked.
        val event = completion(error = null, lastError = "one tool failed")
        assertEquals(RunPhase.Done, completionPhaseFor(event))
        assertTrue(cards(event).isEmpty())
    }

    @Test
    fun `a genuine failure spawns both an Error phase and an ErrorCard`() {
        val event = completion(error = "boom")
        assertEquals(RunPhase.Error, completionPhaseFor(event))
        assertEquals(listOf("boom"), cards(event).map { it.message })
    }
}
