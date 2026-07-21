package com.mewbo.aura.notify

import com.mewbo.aura.data.model.CompletionPayload
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.UserQuestionAnsweredPayload
import com.mewbo.aura.data.model.UserQuestionPayload
import com.mewbo.aura.data.model.UserQuestionSpec
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Contract for the ONE pure decision the completion watcher makes: given the terminal event a run
 * ended on and whether the user is already seeing it, what (if anything) to announce. The suspend
 * watch + the Android service/notification I/O around it are boundary code, deliberately not tested
 * here (`data/CLAUDE.md`: stub only I/O). A numeric-offset `ts` matches the backend's real
 * `isoformat()` shape — irrelevant to this decision, but the house rule is to never seed a fixture
 * from the bare-`Z` form (`test/CLAUDE.md`).
 */
class RunNotificationControllerTest {

    private val ts = "2026-07-14T00:00:00.000000+00:00"

    private fun completion(error: String? = null, lastError: String? = null) =
        SessionEvent.Completion(ts, CompletionPayload(error = error, lastError = lastError))

    @Test
    fun `a clean completion while backgrounded announces Done`() {
        assertEquals(
            CompletionNotice.Done,
            RunNotificationController.completionNotice(completion(), appVisible = false),
        )
    }

    @Test
    fun `a failed completion while backgrounded announces Failed with the server reason`() {
        val notice = RunNotificationController.completionNotice(completion(error = "boom"), appVisible = false)
        assertTrue(notice is CompletionNotice.Failed)
        assertEquals("boom", (notice as CompletionNotice.Failed).reason)
    }

    @Test
    fun `a foregrounded app suppresses every completion, success or failure`() {
        // The user is looking at chat or the overlay — a notification would be redundant.
        assertNull(RunNotificationController.completionNotice(completion(), appVisible = true))
        assertNull(RunNotificationController.completionNotice(completion(error = "boom"), appVisible = true))
    }

    @Test
    fun `last_error residue on a successful run is Done, never Failed`() {
        // DESIGN.md §6 error-card law: a recovered tool error under an otherwise-successful run is a
        // success — failure keys on `error` alone, never `last_error`.
        assertEquals(
            CompletionNotice.Done,
            RunNotificationController.completionNotice(completion(error = null, lastError = "one tool failed"), appVisible = false),
        )
    }

    @Test
    fun `a stream that ends without a completion announces nothing`() {
        // StreamEnd / StreamError stop the watch but carry no honest run outcome.
        assertNull(RunNotificationController.completionNotice(SessionEvent.StreamEnd, appVisible = false))
        assertNull(RunNotificationController.completionNotice(SessionEvent.StreamError(message = "socket closed"), appVisible = false))
        assertNull(RunNotificationController.completionNotice(terminal = null, appVisible = false))
    }

    // --- ask-user questions: the pending-question alert decision (hard requirement) ---

    private fun userQuestion(vararg questions: String): SessionEvent.UserQuestion =
        SessionEvent.UserQuestion(
            ts,
            UserQuestionPayload(
                callId = "q1",
                callToken = "tok1",
                questions = questions.map { UserQuestionSpec(header = "H", question = it) },
            ),
        )

    @Test
    fun `a user_question while backgrounded asks with the first question as the body`() {
        val notice = RunNotificationController.questionNotice(userQuestion("Which auth method?"), appVisible = false)
        assertTrue(notice is QuestionNotice.Ask)
        assertEquals("Which auth method?", (notice as QuestionNotice.Ask).body)
    }

    @Test
    fun `a multi-question group summarises the extras in the alert body`() {
        val notice = RunNotificationController.questionNotice(userQuestion("First?", "Second?", "Third?"), appVisible = false)
        assertEquals("First? (+2 more)", (notice as QuestionNotice.Ask).body)
    }

    @Test
    fun `a foregrounded app suppresses the question alert - the card is already on screen`() {
        assertNull(RunNotificationController.questionNotice(userQuestion("Which?"), appVisible = true))
    }

    @Test
    fun `an answered event clears the alert regardless of who answered or app visibility`() {
        val answered = SessionEvent.UserQuestionAnswered(ts, UserQuestionAnsweredPayload(callId = "q1", outcome = "answered"))
        assertEquals(QuestionNotice.Clear, RunNotificationController.questionNotice(answered, appVisible = false))
        assertEquals(QuestionNotice.Clear, RunNotificationController.questionNotice(answered, appVisible = true))
    }

    @Test
    fun `non-question events never touch the alert`() {
        assertNull(RunNotificationController.questionNotice(completion(), appVisible = false))
        assertNull(RunNotificationController.questionNotice(SessionEvent.StreamEnd, appVisible = false))
    }
}
