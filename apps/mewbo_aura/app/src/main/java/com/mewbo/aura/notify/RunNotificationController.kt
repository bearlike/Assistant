package com.mewbo.aura.notify

import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.UserQuestionPayload
import com.mewbo.aura.data.repo.RunRepository
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.transformWhile

/**
 * What (if anything) a finished turn should announce. A closed union so [RunNotifier] draws each
 * with an exhaustive `when` and a new outcome fails compilation rather than silently falling through
 * (house atomic-class rule). Deliberately NOT carrying the `done_reason` string: the notification is
 * a glance surface, and "finished" vs "couldn't finish" is the whole of what a backgrounded user
 * needs — the detail is in the session they tap through to.
 */
sealed interface CompletionNotice {
    /** The run reached its terminal marker with no orchestrator failure. */
    data object Done : CompletionNotice

    /** The run failed ([CompletionPayload.error] set). [reason] is the server's short message, shown
     * only if non-blank. */
    data class Failed(val reason: String?) : CompletionNotice
}

/**
 * What a stream event means for the pending-question alert ([RunNotifier.postQuestion]). A closed
 * union so [RunNotificationController.questionNotice] returns it exhaustively.
 */
sealed interface QuestionNotice {
    /** A `user_question` arrived while the app is backgrounded — post the alert with this [body]. */
    data class Ask(val body: String) : QuestionNotice

    /** The question resolved (`user_question_answered`) — clear any showing alert. */
    data object Clear : QuestionNotice
}

/**
 * The ONE atomic class owning the turn-completion watch/notify behavior. [RunNotificationService]
 * (the Android foreground-service shell) launches [watchAndNotify] per active run and owns nothing
 * but the service lifecycle; the decision of WHETHER to notify, and posting it, live here.
 *
 * It follows the SAME shared seam every other run-follower uses — [RunRepository.live] — so it
 * shares the one multicast SSE connection chat and the overlay already ride (that sharing is why
 * [RunRepository] is a `@Singleton`; a non-shared instance would open a redundant second stream for
 * every backgrounded run). It is purely a passive collector: it never advertises or answers device
 * tools (that is built into `live()`'s pipeline once, upstream of the multicast), so a second
 * follower can't double-execute one — the singleton `DeviceToolExecutor`'s ledger dedupes anyway.
 *
 * Suppression reuses the existing [AppForegroundChecker] verbatim (foreground-importance OR a
 * showing assist overlay). That is exactly right for "is the user already seeing this result": a
 * visible activity reads importance `IMPORTANCE_FOREGROUND` (100); a showing overlay renders the
 * response live; and this service's OWN foreground state pins importance at `IMPORTANCE_FOREGROUND_SERVICE`
 * (125), which is correctly NOT foreground here (`125 <= 100` is false), so watching never suppresses
 * its own notification.
 */
@Singleton
class RunNotificationController @Inject constructor(
    private val runRepository: RunRepository,
    private val notifier: RunNotifier,
    private val appForegroundChecker: AppForegroundChecker,
) {
    /**
     * Follows [sessionId]'s live stream until the run reaches a terminal event, then decides and
     * posts. Returns when the watch is over (terminal seen, stream ended, or the upstream errored) —
     * the service uses that return to reap itself. Suspends for the whole run, so the caller MUST run
     * it on a foreground-service-backed scope, or Android will throttle the process and the terminal
     * event never arrives (the entire reason this feature needs a service, not a bare coroutine).
     *
     * [label] is the query text captured at send time, shown as the notification body so the user
     * knows WHICH request finished without a title round-trip (a fresh overlay session has no title
     * yet). Empty for a retry, where there is no fresh query text.
     */
    suspend fun watchAndNotify(sessionId: String, label: String) {
        var terminal: SessionEvent? = null
        // One pass over the SAME live stream, servicing BOTH the mid-run question alert and the
        // end-of-run completion. `transformWhile` emits every event through then STOPS pulling once it
        // sees the run terminal (the FGS reaps on return) — a blocked `ask_user_question` is not
        // terminal, so the watch stays alive across the block. The question alert is posted/cleared as
        // events flow; the completion is decided from the captured `terminal` afterward.
        runRepository.live(sessionId)
            .transformWhile { event ->
                emit(event)
                !event.isRunTerminal()
            }
            .collect { event ->
                when (val notice = questionNotice(event, appVisible = appForegroundChecker.isForeground())) {
                    is QuestionNotice.Ask -> notifier.postQuestion(sessionId, notice.body)
                    QuestionNotice.Clear -> notifier.cancelQuestion(sessionId)
                    null -> Unit
                }
                if (event.isRunTerminal()) terminal = event
            }
        // The run ended — clear any still-showing question alert (idempotent), then announce completion.
        notifier.cancelQuestion(sessionId)
        val notice = completionNotice(terminal, appVisible = appForegroundChecker.isForeground())
        if (notice != null) notifier.postCompletion(sessionId, label, notice)
    }

    companion object {
        /** Stops the watch. Includes the synthetic [SessionEvent.StreamError] and the control
         * [SessionEvent.StreamEnd] so a stream that ends WITHOUT a `completion` (a dropped terminal
         * frame under buffer pressure, or an upstream throw) still releases the service instead of
         * pinning a zombie foreground service — only [SessionEvent.Completion] actually produces a
         * notification ([completionNotice]). */
        private fun SessionEvent.isRunTerminal(): Boolean =
            this is SessionEvent.Completion ||
                this is SessionEvent.StreamEnd ||
                this is SessionEvent.StreamError

        /**
         * The pure decision (unit-tested): given the terminal event a run ended on and whether the
         * app is already visible to the user, what should be announced — or `null` to stay silent.
         *
         * - `appVisible` ⇒ always `null`: the user is looking at chat or the overlay, so a
         *   notification would be redundant (hard requirement).
         * - Only [SessionEvent.Completion] announces. [SessionEvent.StreamEnd]/[SessionEvent.StreamError]
         *   stop the watch but carry no honest run outcome, so they announce nothing.
         * - Failure keys on `error` ALONE, never `last_error`: a successful run carrying `last_error`
         *   residue from one recovered tool call is a success (DESIGN.md §6 error-card law — never
         *   surface tool-error residue as a failure), so it announces [CompletionNotice.Done].
         */
        fun completionNotice(terminal: SessionEvent?, appVisible: Boolean): CompletionNotice? {
            if (appVisible) return null
            val completion = terminal as? SessionEvent.Completion ?: return null
            val error = completion.payload.error
            return if (error != null) CompletionNotice.Failed(error) else CompletionNotice.Done
        }

        /**
         * The pure decision (unit-tested) for the pending-question alert: what a stream event means.
         *
         * - A `user_question` while backgrounded ⇒ [QuestionNotice.Ask] — the whole point is alerting a
         *   user AWAY from the app that a blocked run is waiting. `appVisible` ⇒ `null` (suppress): the
         *   interactive question card is already on screen, same suppression rule as [completionNotice].
         * - A `user_question_answered` (answered here OR on another surface) ⇒ [QuestionNotice.Clear].
         * - Every other event ⇒ `null` (no change); the run terminal is handled separately by the watch,
         *   which clears any outstanding alert unconditionally on the way out.
         */
        fun questionNotice(event: SessionEvent, appVisible: Boolean): QuestionNotice? = when (event) {
            is SessionEvent.UserQuestion ->
                if (appVisible) null else QuestionNotice.Ask(questionNotificationBody(event.payload))
            is SessionEvent.UserQuestionAnswered -> QuestionNotice.Clear
            else -> null
        }

        /** The alert body for a pending question: the first question's text (the actionable glance —
         * the title already says a question is waiting), with a "(+N more)" suffix when the group holds
         * several. An empty group falls back to a generic prompt. */
        fun questionNotificationBody(payload: UserQuestionPayload): String {
            val first = payload.questions.firstOrNull() ?: return "Mewbo needs your input to continue."
            val extra = payload.questions.size - 1
            return if (extra > 0) "${first.question} (+$extra more)" else first.question
        }
    }
}
