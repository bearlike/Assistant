package com.mewbo.aura.notify

import android.os.SystemClock
import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.Timestamps
import com.mewbo.aura.data.model.UserQuestionPayload
import com.mewbo.aura.data.repo.RunRepository
import java.time.Instant
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.transformWhile
import kotlinx.coroutines.launch

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
class RunNotificationController(
    private val live: (String) -> Flow<SessionEvent>,
    private val notifier: RunNotifier,
    private val appForegroundChecker: AppForegroundChecker,
    private val nowMs: () -> Long,
    private val holdIdleBoundMs: Long,
    private val pollIdleMs: Long,
) {
    /**
     * The production shape. The primary constructor above takes the two DURATIONS and the CLOCK as
     * arguments — the same reason `VeilFade` does ([`ui/control/`](../ui/control/CLAUDE.md)): the
     * behaviour under test is a 15-minute bound, and a test that had to sleep through it would
     * either be skipped or shortened until it proved nothing. It also takes [live] as a lambda
     * rather than the repository, so a plain-JVM test can script the stream without constructing a
     * [RunRepository], whose SSE collaborators are not JVM-constructible.
     *
     * `SystemClock.elapsedRealtime` and not `System.currentTimeMillis`: the bound measures an
     * ELAPSED interval, and a wall clock can step (NTP, a user changing the time) in the direction
     * that either ends a live grant early or extends an abandoned one indefinitely. `elapsedRealtime`
     * is monotonic and keeps counting through deep sleep, which is where a backgrounded hold spends
     * most of its life. It is the one Android reference in this file and no test reaches it — the
     * primary constructor is what a test calls.
     */
    @Inject
    constructor(
        runRepository: RunRepository,
        notifier: RunNotifier,
        appForegroundChecker: AppForegroundChecker,
    ) : this(
        live = runRepository::live,
        notifier = notifier,
        appForegroundChecker = appForegroundChecker,
        nowMs = SystemClock::elapsedRealtime,
        holdIdleBoundMs = HOLD_IDLE_BOUND_MS,
        pollIdleMs = POLL_IDLE_MS,
    )

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
        watchAndNotify(sessionId, label, holdForDeviceControl = false)
    }

    /**
     * As above, but [holdForDeviceControl] keeps the subscription open past the
     * run's terminal event.
     *
     * **This collector IS what keeps device tools answerable while the user is
     * in another app.** Dispatch is a step in `live()`'s pipeline, and that
     * pipeline stops 5s after its last subscriber leaves. Chat's collector dies
     * the moment Aura is backgrounded — which is precisely what
     * `device_action(action="launch")` does, by design, on its way to the app it
     * was told to open. So the tool that navigates destroys the transport for
     * the tools that follow it, and every later call fails: first as a 30s
     * timeout while the server still counts a frozen subscriber, then instantly
     * as unavailable once it is reaped.
     *
     * Holding here does not extend a RUN; it extends the CHANNEL. The run ends
     * when it ends, the notification is posted as before, and the subscription
     * stays so the next turn of a device-control session can still reach the
     * phone.
     *
     * **It ends when the stream goes SILENT for [holdIdleBoundMs], and that
     * return is what releases the grant** ([RunNotificationService.onWatchEnded]).
     * Before the bound existed the hold's loop had no exit condition at all: it
     * returned only by cancellation, so a grant taken by a run whose session then
     * died — the backend losing the connection, or the model concluding the
     * session server-side, neither of which reaches this client as an event —
     * outlived everything, and 6 of 24 measured grant windows never saw
     * `device_control_stop`.
     */
    suspend fun watchAndNotify(sessionId: String, label: String, holdForDeviceControl: Boolean) {
        // Advanced by genuine session PROGRESS, never by the act of reconnecting — see
        // [StreamIdleClock], which is where the bound's whole correctness now lives.
        val idleClock = StreamIdleClock(nowMs)

        if (!holdForDeviceControl) {
            // One pass over the SAME live stream, servicing BOTH the mid-run question alert and the
            // end-of-run completion. `transformWhile` emits every event through then STOPS pulling
            // once the epoch's stopping event arrives — a blocked `ask_user_question` is not
            // terminal, so the watch stays alive across the block.
            watchOneEpoch(sessionId, label, holdForDeviceControl = false, announcedTs = null, idleClock)
            return
        }

        // **The watchdog covers the FIRST epoch as well as the rebuild loop, and that is not
        // symmetry for its own sake.** A connection lost mid-run never produces a `stream_end` and
        // never produces a `stream_error` either — `SessionStreamClient` swallows the `IOException`
        // and reconnects forever with a 15s ceiling — so the first epoch is precisely where a dead
        // channel hangs indefinitely. A bound checked only between epochs would never be reached.
        coroutineScope {
            val hold = launch {
                // A device-control session outlives its run, so the CHANNEL has to be rebuilt, not
                // merely held. See [holdChannelOpen] — a plain `collect {}` cannot do this, and
                // measurably did not.
                val announced =
                    watchOneEpoch(sessionId, label, holdForDeviceControl = true, announcedTs = null, idleClock)
                holdChannelOpen(sessionId, label, announced, idleClock)
            }
            awaitIdleBound(idleClock)
            hold.cancel()
            hold.join()
        }
    }

    /**
     * Returns once the stream has delivered nothing for [holdIdleBoundMs] — the honest reading of
     * "nobody is coming back".
     *
     * **It sleeps exactly as long as the remaining budget, then re-reads.** An event arriving during
     * that sleep pushes [lastEventAt] forward, so the next pass simply sleeps again; a busy grant
     * therefore costs ONE wakeup per bound-length window rather than a poll per event.
     *
     * **Deliberately not a server liveness read.** `GET /sessions/{id}/events?after=` returns
     * authoritative `running`/`terminated`, and asking it each poll was the considered alternative.
     * It was not needed: measured across real grant windows, the largest gap between events inside a
     * LIVE grant is ~4.3 minutes and the smallest gap inside a LEAKED one is 15.7 hours. Nothing
     * falls between, so a purely local bound separates the two cases with a margin no round trip
     * would improve — and a REST call every [pollIdleMs] for every held grant is cost on an
     * interactive path bought for nothing.
     */
    private suspend fun awaitIdleBound(idleClock: StreamIdleClock) {
        while (true) {
            val idleFor = idleClock.idleForMs()
            if (idleFor >= holdIdleBoundMs) return
            delay(holdIdleBoundMs - idleFor)
        }
    }

    /**
     * Collects one channel epoch: a single subscription to [RunRepository.live], from connect to the
     * event that ends it. Returns when that event arrives.
     *
     * The stopping event differs by mode, and conflating the two is what broke the hold:
     * - **No hold** — the FIRST terminal, `completion` included. The service reaps on return, which
     *   is the whole point of a completion watch.
     * - **Hold** — only the TRANSPORT terminal (`stream_end`/`stream_error`). A `completion` means
     *   this TURN finished, not that the connection did; stopping there leaves the epoch's real end
     *   unobserved, which is precisely how the hold used to latch onto a dead stream.
     *
     * The completion notification is posted from INSIDE the collect, on the `completion` event
     * itself, so both modes announce identically — [completionNotice] answers `null` for every other
     * terminal anyway, so this is the same decision, taken where both paths can reach it.
     *
     * [announcedTs] is the `ts` of a completion already announced, and it is load-bearing across
     * epochs: the reconnect cursor is INCLUSIVE, so an epoch that ended ON a `completion` has that
     * same event re-delivered as the first frame of the next one. Without this the user would get a
     * fresh "finished" notification every poll for the rest of the hold. Returns the ts of the
     * newest completion seen, to be carried into the next epoch.
     */
    private suspend fun watchOneEpoch(
        sessionId: String,
        label: String,
        holdForDeviceControl: Boolean,
        announcedTs: String?,
        idleClock: StreamIdleClock,
    ): String? {
        var announced = announcedTs
        live(sessionId)
            .transformWhile { event ->
                emit(event)
                if (holdForDeviceControl) !event.isTransportTerminal() else !event.isRunTerminal()
            }
            .collect { event ->
                // The one stamp the idle bound reads, and it is deliberately NOT "an event arrived".
                // Every event still counts — narrowing to, say, `device_tool_call` would reap a
                // grant during a long stretch of ordinary reasoning steps — but only a NEWER one
                // does. See [StreamIdleClock]: three separate frames arrive on every rebuild of a
                // dead channel, so an arrival stamp reset the bound every poll and the bound could
                // never be reached at all.
                idleClock.markIfAdvanced(event.ts)
                when (val notice = questionNotice(event, appVisible = appForegroundChecker.isForeground())) {
                    is QuestionNotice.Ask -> notifier.postQuestion(sessionId, notice.body)
                    QuestionNotice.Clear -> notifier.cancelQuestion(sessionId)
                    null -> Unit
                }
                if (event is SessionEvent.Completion && event.ts != announced) {
                    // The run ended — clear any still-showing question alert (idempotent), then announce.
                    announced = event.ts
                    notifier.cancelQuestion(sessionId)
                    val notice = completionNotice(event, appVisible = appForegroundChecker.isForeground())
                    if (notice != null) notifier.postCompletion(sessionId, label, notice)
                }
            }
        notifier.cancelQuestion(sessionId)
        return announced
    }

    /**
     * Keeps a device-control session's command channel reachable after its run ends, by REBUILDING
     * the subscription rather than holding one open.
     *
     * **Why a plain `collect {}` cannot work, measured rather than reasoned.** The server closes an
     * idle session's stream within milliseconds — it reads liveness before choosing a blocking
     * timeout, so a session with no run in flight gets `stream_end` immediately. That completes the
     * cold upstream, and a `shareIn` whose upstream has completed never restarts: `SharedFlow.collect`
     * then suspends forever on a flow that will never emit again. Observed on device: the persistent
     * "Mewbo can control this device" notification showing, the service reporting `isForeground=true`,
     * and the process holding ZERO TCP sockets for as long as it was sampled. The hold was holding
     * nothing, and because its job was still parked in the service's watch map, the next turn's start
     * was dropped as a duplicate — so the channel could never come back on its own.
     *
     * Rebuilding is affordable only because a reconnect carries an `after` cursor, which makes the
     * replay empty; that is the server's own stated expectation for this exact pattern. [POLL_IDLE_MS]
     * then paces the loop: without it, a stream the server closes in milliseconds would be reopened in
     * milliseconds, spending one of the API's few request slots in a hot loop. A turn that starts while
     * we are between epochs is not lost — the next connect replays from the cursor, so its
     * `device_tool_call` arrives on the new subscription.
     *
     * Cancelled with the service — the user's Stop and the hold's own reap alike — and by
     * [awaitIdleBound] when the stream has been silent long enough that nobody is coming back. It is
     * never left running.
     */
    private suspend fun holdChannelOpen(
        sessionId: String,
        label: String,
        announcedTs: String?,
        idleClock: StreamIdleClock,
    ) {
        var announced = announcedTs
        while (true) {
            delay(pollIdleMs)
            announced =
                watchOneEpoch(sessionId, label, holdForDeviceControl = true, announcedTs = announced, idleClock)
        }
    }

    companion object {
        /**
         * How long the hold waits between channel epochs.
         *
         * The server closes an idle session's stream immediately, so this is what stands between a
         * correct rebuild and a hot reconnect loop against an API whose request slots are few and
         * shared. It is a PACING floor, not a latency budget: a turn starting mid-wait is picked up
         * by the next epoch's cursor-trimmed replay, so nothing is missed by waiting — only noticed a
         * little later. Long enough to be cheap over a two-hour hold, short enough that a device call
         * is answered well inside the server's own tool-call timeout.
         */
        private const val POLL_IDLE_MS = 15_000L

        /**
         * How long a device-control hold may see NOTHING on the stream before it ends itself, and
         * with it the grant.
         *
         * **The number is measured, and the margin around it is the justification — do not retune
         * it without re-running the query.** Over real grant windows (the events between a
         * `device_control_start` and its `device_control_stop`, read from the session event store):
         *
         * - 6 of 24 windows never saw a `device_control_stop` at all. The leak is ~25% of grants.
         * - Gaps between events INSIDE a held grant: n=1993, p50 0.0s, p90 3.8s, p99 24.3s.
         * - The eight largest gaps: 88.7, 92.2, 157.8, 180.0, 256.0, 56627.6, 78992.3, 95239.7
         *   seconds.
         *
         * That distribution is cleanly bimodal: the largest plausible LIVE gap is 256s (~4.3 min)
         * and the next value up is 15.7 HOURS, with nothing in between. 15 minutes sits ~3.5× above
         * the worst observed live gap and ~60× below the smallest leaked one, so no plausible
         * retuning inside that gap changes which side any observed window falls on. The longest
         * legitimately-closed window ran 20.1 minutes — longer than this bound, and correctly
         * unaffected, because the bound measures SILENCE, never total duration.
         *
         * **Why it is now EIGHT minutes and not fifteen.** Fifteen was chosen when this bound was
         * believed to be the working release; re-measured against the same event store it is not
         * the number that was wrong, it is that the bound was never REACHED (see [StreamIdleClock]).
         * With the stamp corrected, the margin arithmetic is what picks the value: 8 minutes is
         * 1.9× the largest gap ever observed inside a LIVE grant (256.0s) and ~118× below the
         * smallest gap inside a leaked one (56627.6s = 15.7h). Nothing observed falls between, so
         * the shorter bound moves no measured window to the wrong side — and fifteen minutes of a
         * dead overlay on somebody's television reads as "stuck", which is the report this closes.
         *
         * Distinct from [RunNotificationService]'s caps, which stay: those bound how long a hold or
         * a grant may exist AT ALL, this one bounds how long it may exist with nothing happening.
         */
        private const val HOLD_IDLE_BOUND_MS = 8L * 60L * 1000L

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
         * Ends a channel EPOCH, which is a different question from whether the RUN ended.
         *
         * Only the transport's own terminals count: a `completion` says this turn finished while the
         * connection carrying it is still perfectly good, and a device-control session expects more
         * turns on it. Treating a `completion` as the end of the epoch is what left the hold
         * subscribed to a stream that had already gone.
         */
        private fun SessionEvent.isTransportTerminal(): Boolean =
            this is SessionEvent.StreamEnd || this is SessionEvent.StreamError

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

/**
 * How long a held channel has gone without the session making PROGRESS — the one reading the
 * device-control hold's idle bound is decided on.
 *
 * **"An event arrived" is not progress, and believing it was is what made the bound unreachable.**
 * The hold rebuilds its subscription every poll (`POLL_IDLE_MS`, 15s),
 * because the server closes an idle session's stream in milliseconds. Every one of those rebuilds
 * delivers frames the reconnect itself produced, on a session where nothing whatsoever has
 * happened:
 *
 * - a `session_state` frame, yielded unconditionally on connect (`backend.py`'s stream generator),
 * - a `stream_end` frame, yielded immediately because `is_running` is false, and
 * - the newest real event again, because the server's `after` cursor is INCLUSIVE by design.
 *
 * So a stamp taken on ARRIVAL was refreshed roughly three times every fifteen seconds for as long
 * as the hold lived. `idleFor` could never exceed one poll interval, the fifteen-minute bound was
 * never once reached on any device, and the only thing left releasing an abandoned grant was a
 * two-hour ceiling that each new run pushed further out. That is the leak the device owner reported
 * as an overlay only a force-stop could remove.
 *
 * **The cure is to read the SERVER's clock, not ours.** Progress is the newest event `ts`
 * ADVANCING. A re-delivered duplicate carries the same `ts` and advances nothing; `stream_end` and
 * the synthesized `StreamError` carry no `ts` at all; `session_state` carries none either. Only a
 * genuinely new event moves the mark, which is exactly the fact the bound wants and the only one
 * a reconnect cannot manufacture.
 *
 * Timestamps go through [Timestamps.parseInstantOrNull], never a bare `Instant.parse` — the backend
 * emits a numeric offset that Android's bundled `java.time` refuses. An unparseable or absent `ts`
 * is "no progress", which is the direction that lets the bound still fire; the service's grant
 * ceiling is what covers being wrong about that.
 *
 * The clock is [nowMs] (production: `SystemClock.elapsedRealtime`, monotonic and counting through
 * deep sleep), injected for the same reason the durations are: a bound measured in minutes must be
 * assertable in milliseconds of virtual time.
 */
internal class StreamIdleClock(private val nowMs: () -> Long) {
    private val newestTs = AtomicReference<Instant?>(null)
    private val lastAdvanceAt = AtomicLong(nowMs())

    /**
     * Records [ts] and reports whether it was NEWER than anything seen — the only thing that resets
     * the idle reading.
     *
     * The compare-and-set loop is not ceremony: the hold and its idle watchdog are two coroutines
     * that may land on different threads, and a lost update here reads as progress that did not
     * happen.
     */
    fun markIfAdvanced(ts: String): Boolean {
        val parsed = Timestamps.parseInstantOrNull(ts) ?: return false
        while (true) {
            val current = newestTs.get()
            if (current != null && !parsed.isAfter(current)) return false
            if (newestTs.compareAndSet(current, parsed)) {
                lastAdvanceAt.set(nowMs())
                return true
            }
        }
    }

    /** Milliseconds since the session last made progress. Counted from construction until the first
     * advance, so a hold whose stream never says anything at all is bounded from the moment it
     * starts rather than never. */
    fun idleForMs(): Long = nowMs() - lastAdvanceAt.get()
}
