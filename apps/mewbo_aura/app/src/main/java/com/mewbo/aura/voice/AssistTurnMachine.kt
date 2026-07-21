package com.mewbo.aura.voice

import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.data.model.Timestamps
import com.mewbo.aura.data.model.TranscriptReducer
import java.time.Duration
import java.time.Instant
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.consumeEach
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.produceIn
import kotlinx.coroutines.launch

/**
 * The v5 assist-overlay state machine, owned by [com.mewbo.aura.voice.AuraSession].
 * A PLAIN atomic class (state as constructor-injected attributes, behavior as methods) - not a
 * `ViewModel`, since a `VoiceInteractionSession` isn't a `ViewModelStoreOwner` host and this needs
 * to be driven with a caller-supplied [scope]/deps for unit testing anyway.
 *
 * Voice-first, first-turn-in-overlay (supersedes v4's text-first, handoff-only contract - see
 * [AssistUiState]'s KDoc): the FIRST turn of an invocation streams its response into the overlay as
 * a card ([AssistUiState.Streaming], folding [liveEvents] through [TranscriptReducer] exactly like
 * the pre-v4 machine did - recovered from `git show f7e505a:.../AssistTurnMachine.kt` for this spec,
 * never a fork of that reducer logic); every turn AFTER the first keeps the v4 dispatch-then-handoff
 * path. [createSession]/[sendQuery]/[liveEvents]/[onHandoff] are the narrow CALL SHAPES of
 * [com.mewbo.aura.data.repo.SessionRepository]/[com.mewbo.aura.data.repo.RunRepository]/the host's
 * own app-launch, not the concrete classes themselves - those classes are `final` and transitively
 * require Android Keystore (`SettingsStore` -> `KeystoreCipher`) and OkHttp `EventSource.Factory`
 * (`SessionStreamClient`), none of which are constructible on a plain JVM unit test runner without
 * Robolectric (`data/CLAUDE.md`). The production wiring in [com.mewbo.aura.voice.AuraSession] just
 * passes the real singletons' methods straight through; tests pass trivial lambdas - no fakes or
 * mocking library needed for the repo boundary at all.
 *
 * [transcriber] + [synthesizer] and the full [startListening] capture loop are UNCHANGED from v4 -
 * only the CALLER changed (v5 auto-listen lives in `AuraSession.onShow()`, right after
 * [show], gated on `RECORD_AUDIO` the same way the mic tap already was - the machine itself still
 * doesn't know or care how [startListening] got invoked).
 */
class AssistTurnMachine(
    private val transcriber: Transcriber,
    private val synthesizer: Synthesizer,
    private val sessions: StateFlow<List<SessionSummary>>,
    private val createSession: suspend () -> String,
    private val sendQuery: suspend (sessionId: String, text: String) -> Unit,
    /** [com.mewbo.aura.data.repo.RunRepository.live]'s narrow call shape - the ONE
     * turn that streams in-overlay ([subscribeLive]) folds this through [TranscriptReducer] itself,
     * the same accumulation [com.mewbo.aura.ui.chat.ChatViewModel] trusts, so the card reuses the
     * shared `ChatTranscript` composable rather than a second rendering path. */
    private val liveEvents: (sessionId: String) -> Flow<SessionEvent>,
    /** Fired once a turn's query has actually been dispatched - for [continueLastSession] and
     * [expand], immediately (no query dispatched by either). [modality] tags which
     * input channel originated the turn being handed off, defaulting to whatever started the FIRST
     * turn of this invocation (see [turnModality]). Client-only; the caller (`AuraSession`) is what
     * puts it on the handoff `Intent` extra - nothing about it reaches the backend. */
    private val onHandoff: (sessionId: String, modality: InputModality) -> Unit,
    private val scope: CoroutineScope,
    private val haptics: AuraHaptics = NoOpAuraHaptics,
    /**
     * Fire-and-forget refresh of [sessions]' backing store (carried over from v3): the assist path
     * is often a cold process start from the assistant gesture, so
     * [com.mewbo.aura.data.repo.SessionRepository]'s cache is still the initial empty list and
     * "continue last session" has nothing to offer even once [recentSessionOrNull] itself parses
     * correctly. Triggered once, on the first [show] or [startListening]; failures (offline, etc.)
     * are swallowed - the overlay must never depend on this succeeding.
     */
    private val refreshSessions: suspend () -> Unit = {},
    /** The pull-up (swipe-up on the composer pill) handoff seam (user directive 2026-07-04), kept
     * distinct from [onHandoff] because it carries the routing DECISION's two extra facts: a
     * NULLABLE [sessionId] ([expand]/[onHandoff] always have one, a pull-up before the first turn has
     * none -> land the app on a fresh new chat) and the composer's typed-but-unsent [draft] (null
     * when blank) for the in-app composer to seed from. The host (`AuraSession`) turns this into the
     * SAME `MainActivity` launch [onHandoff] does - session id present => route to that session (the
     * existing second-interaction path, reused verbatim), absent => the app's default new chat.
     * Client-only, never a wire concern; the default no-op keeps non-overlay constructions (tests)
     * terse. */
    private val onPullUp: (sessionId: String?, draft: String?, modality: InputModality) -> Unit = { _, _, _ -> },
) {
    private val _state = MutableStateFlow<AssistUiState>(AssistUiState.Idle)
    val state: StateFlow<AssistUiState> = _state.asStateFlow()

    /** Speak-along: owns its OWN [SpeechController] instance rather than sharing
     * [com.mewbo.aura.ui.chat.ChatViewModel]'s - the overlay's in-card turn and the app's chat turn
     * are never the same live turn at once (the overlay always hands off or tears down before the
     * app's own `ChatViewModel.bind` ever attaches), so there is no state to reconcile between two
     * instances, just two call SITES of the same shared class (moved from `ui/chat` to `voice/` for
     * this spec - "all speech in voice/", `voice/CLAUDE.md`). */
    private val speech = SpeechController(synthesizer, scope)

    private var sessionsRefreshTriggered = false

    private var listenJob: Job? = null
    private var silenceJob: Job? = null
    private var sendJob: Job? = null
    private var streamJob: Job? = null

    /** `0` before any turn has been dispatched by this invocation; flips to `1` the instant the
     * FIRST turn's query is dispatched successfully - every later [beginTurn] takes the v4
     * dispatch-then-handoff path instead of streaming in-overlay ("second interaction
     * hands off"). Reset to `0` in [dismiss]: the underlying `VoiceInteractionSession` (and this
     * SAME machine instance) survives hide->show on a real device (confirmed against AOSP
     * `VoiceInteractionManagerServiceImpl` - `mActiveSession` is reused, `onNewSession` fires
     * once), so without this reset a SECOND assistant invocation would
     * silently inherit the previous invocation's turn count and skip straight to handoff on what
     * the user experiences as their first query of a brand new overlay. */
    private var turnCount = 0

    /** The session THIS invocation is turning into - `null` until the first turn's [createSession]
     * resolves, reused (never re-created) by every later turn of the SAME invocation, including a
     * retry after a first-turn [sendQuery] failure (see [beginTurn]). Reset alongside [turnCount]
     * in [dismiss]. */
    private var sessionId: String? = null

    /** Which [InputModality] started the FIRST turn - threads into [expand]'s `onHandoff` call and
     * gates [speech]'s speak-along to voice-initiated turns only. Reset in [dismiss]. */
    private var turnModality: InputModality = InputModality.Text

    /** `true` once [toggleSpeak] mutes the CURRENT invocation's speak-along. Distinct from
     * [SpeechController]'s own `closedKey` latch (its KDoc): THIS flag is what stops [subscribeLive]
     * from feeding it any FUTURE deltas at all; `closedKey` is what stops a barged-in message from
     * resuming mid-sentence even after unmuting. Reset in [dismiss]. */
    private var speechMuted = false

    private var reducerState = TranscriptReducer.State()

    init {
        // Keeps the Streaming card's `speaking` flag live between transcript folds (a text delta
        // and a synthesizer Done/Error don't arrive on the same cadence) - guarded to ONLY apply
        // while genuinely mid-stream, so a late speakingKey flip can never resurrect `speaking` on
        // a card [subscribeLive] has already marked `done` (see [Streaming.speaking]'s own KDoc).
        scope.launch {
            speech.speakingKey.collect { key ->
                val current = _state.value
                if (current is AssistUiState.Streaming && !current.done) {
                    _state.value = current.copy(speaking = key != null)
                }
            }
        }
    }

    /** `AuraSession.onShow()`'s entry point: the overlay becomes visible in the resting READY
     * state. Auto-listen is the CALLER's decision, made right after this returns
     * (`RECORD_AUDIO` gate - a `VoiceInteractionSession` has no Activity to run the normal
     * permission-request flow through, same reasoning as the pre-v5 mic tap gate) - this method
     * itself never starts capture on its own. */
    fun show() {
        triggerSessionsRefreshOnce()
        setState(readyState())
    }

    /** Voice capture entry point - called automatically right after [show] when `RECORD_AUDIO` is
     * granted (auto-listen), or reachable via an explicit mic tap same as before; the
     * capture loop itself is unchanged either way. */
    @OptIn(ExperimentalCoroutinesApi::class)
    fun startListening() {
        listenJob?.cancel()
        silenceJob?.cancel()
        speech.bargeIn() // barge-in: starting a new LISTENING turn stops any in-flight speech.
        triggerSessionsRefreshOnce()

        setState(AssistUiState.Listening(partial = "", rmsDb = 0f))
        resetSilenceTimer()

        listenJob = scope.launch {
            var finalized = false
            var lastPartial = ""
            val channel = transcriber.listen().produceIn(this)
            try {
                channel.consumeEach { event ->
                    if (finalized) return@consumeEach
                    // Reset the silence timer only on EVIDENCE of speech (Ready, or a Partial whose
                    // text actually changed) - never on Rms, which fires continuously throughout the
                    // whole session on real hardware (SpeechRecognizer#onRmsChanged) regardless of
                    // whether the user is speaking.
                    when (event) {
                        TranscriberEvent.Ready -> resetSilenceTimer()
                        is TranscriberEvent.Rms -> updateListening { it.copy(rmsDb = event.db) }
                        is TranscriberEvent.Partial -> {
                            updateListening { it.copy(partial = event.text) }
                            if (event.text != lastPartial) {
                                lastPartial = event.text
                                resetSilenceTimer()
                            }
                        }
                        is TranscriberEvent.Final -> {
                            finalized = true
                            silenceJob?.cancel()
                            if (event.text.isBlank()) {
                                setState(readyState())
                            } else {
                                haptics.transcriptAccepted() // §6.13: voice Final accepted, about to beginTurn.
                                beginTurn(event.text, InputModality.Voice)
                            }
                        }
                        is TranscriberEvent.Error -> {
                            finalized = true
                            silenceJob?.cancel()
                            haptics.listeningEnded() // §6.13/R4: capture ended without an accepted transcript.
                            setState(readyState()) // NoMatch/Timeout/Unavailable: quiet return (§9.1).
                        }
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } finally {
                channel.cancel()
            }
        }
    }

    /** Mic tap (or auto-listen) when `RECORD_AUDIO` isn't OS-granted: a `VoiceInteractionSession`
     * has no Activity to run the normal first-tap permission-request flow through, so
     * [AuraSession] (the one place that actually holds a [android.content.Context]) calls this
     * instead of [startListening] - same quiet §6.12 notice a failed send already uses
     * ([AssistUiState.Error]), rather than silently doing nothing. No retry text: there's nothing
     * queued to resend. Auto-listen itself stays silent on a denied grant (the overlay
     * simply stays [AssistUiState.Ready] text-first) - this notice is reserved for an EXPLICIT mic
     * tap, [AuraSession] is what makes that distinction, not this method. */
    fun microphonePermissionUnavailable() {
        setState(AssistUiState.Error(reason = "Microphone access isn't granted", retryText = ""))
    }

    /** Tap-equivalent cancel of an in-progress voice capture: stop, discard, back to READY - the
     * overlay stays open (see [AssistUiState.Idle]'s KDoc for why this isn't Idle). */
    fun cancelListening() {
        // Only a genuinely-in-progress capture can be cancelled. A stray tap after the turn has
        // already left LISTENING (an accepted Final now Sending/Streaming, or already back at Ready)
        // must NOT fire the end-of-listening cue or reset an in-flight card to Ready.
        if (_state.value !is AssistUiState.Listening) return
        listenJob?.cancel()
        silenceJob?.cancel()
        haptics.listeningEnded() // §6.13/R4: capture ended without an accepted transcript.
        setState(readyState())
    }

    /** Back / swipe-down: full reset, including any in-flight [beginTurn]/[subscribeLive] work (so
     * a handoff or a card update can never fire after the user has already backed out) - and, per
     * [turnCount]'s own KDoc, a full reset of this invocation's turn bookkeeping so a REUSED machine
     * instance starts its next `show()` with a clean "first turn stays in the overlay" contract. The
     * caller (`AuraSession`) additionally hides the window. */
    fun dismiss() {
        listenJob?.cancel()
        silenceJob?.cancel()
        sendJob?.cancel()
        streamJob?.cancel()
        streamJob = null
        speech.bargeIn()
        turnCount = 0
        sessionId = null
        turnModality = InputModality.Text
        speechMuted = false
        reducerState = TranscriptReducer.State()
        setState(AssistUiState.Idle)
    }

    /** Text send from the overlay's composer - reachable both pre-first-turn and as a follow-up
     * typed while the first turn's card is still showing (that follow-up is turn TWO, so it takes
     * the handoff path per [turnCount]'s own KDoc). */
    fun sendText(text: String) {
        val trimmed = text.trim()
        if (trimmed.isBlank()) return
        listenJob?.cancel()
        silenceJob?.cancel()
        beginTurn(trimmed, InputModality.Text)
    }

    /** `ContinueLastSessionChip` tap: a direct handoff to the most recent session, no query
     * dispatched - distinct from [beginTurn], which always targets THIS invocation's own session. A
     * no-op if the recent session has since fallen out of the 24h window. [InputModality.Text] tags
     * this handoff (a tap, not a dispatched turn - see [onHandoff]'s own KDoc). */
    fun continueLastSession() {
        val recent = recentSessionOrNull() ?: return
        haptics.settle() // §6.13: a successful handoff fold, same as a dispatched turn's.
        onHandoff(recent.sessionId, InputModality.Text)
    }

    /** Card stop control: client-side detach only, same v1 semantics as
     * [com.mewbo.aura.ui.chat.ChatViewModel.stop] - the backend run keeps going. Also a barge-in
     * trigger for the speak-along. A no-op outside [AssistUiState.Streaming]. */
    fun stopStreaming() {
        streamJob?.cancel()
        streamJob = null
        speech.bargeIn()
        val current = _state.value
        if (current is AssistUiState.Streaming) setState(current.copy(done = true, speaking = false))
    }

    /** Card speaker control: toggles [speechMuted]. Muting stops whatever's playing
     * and, per [SpeechController.bargeIn]'s own `closedKey` latch, permanently ends speech for the
     * REST of this message - unmuting doesn't retroactively resume mid-sentence (the same contract
     * a manual read-aloud barge-in already has elsewhere in the app); it only re-arms speech for
     * whatever assistant text folds in AFTER the unmute. */
    fun toggleSpeak() {
        speechMuted = !speechMuted
        if (speechMuted) speech.bargeIn()
    }

    /** Expand control: hand off to the app on THIS invocation's session, reachable
     * during OR after streaming (`ChatViewModel.bind` already attaches to an already-live run
     * either way) - a no-op before the first turn's session id has resolved. Does not itself stop
     * streaming or tear down state; the caller's `onHandoff` wiring (`AuraSession`) is what
     * dismisses the window, same as every other handoff path. */
    fun expand() {
        val id = sessionId ?: return
        onHandoff(id, turnModality)
    }

    /** Pull-up (swipe-up on the composer pill, user directive 2026-07-04): softly hand the overlay
     * into the full app from ANY state. THE ROUTING DECISION (the testable seam this whole feature
     * hangs on): [sessionId] != null - THIS invocation already streamed a first turn, so a session
     * exists - hands off ONTO that session, the SAME target [expand] reaches; otherwise it hands off
     * to a fresh new chat (a null session id - the app's default landing). [draft] (the composer's
     * current text) rides along in BOTH cases, blanked to null so the app's composer only seeds from
     * genuinely-typed text. Fires the standard `settle` release haptic every committed handoff does
     * ([continueLastSession]/[beginTurn]'s turn-two branch). Unlike [expand] this is NEVER a no-op: a
     * null session id is the meaningful "open a new chat" instruction, not "nothing to do yet".
     * Teardown is the caller's `onPullUp` wiring (`AuraSession` dismisses the window), same as every
     * other handoff path - this method itself neither cancels in-flight work nor resets state. */
    fun pullUpToApp(draft: String) {
        haptics.settle()
        onPullUp(sessionId, draft.trim().ifBlank { null }, turnModality)
    }

    /** Ensure THIS invocation's session, dispatch [text] as a query, then either stream the FIRST
     * response in-overlay ([subscribeLive]) or hand off to the app (every turn after) - see
     * [turnCount]'s own KDoc. [sessionId] is resolved via `?:` rather than unconditionally created
     * so a retry after a first-turn [sendQuery] failure (session created, dispatch failed) reuses
     * the SAME session instead of orphaning a new one. */
    private fun beginTurn(text: String, modality: InputModality) {
        setState(AssistUiState.Sending)
        val isFirstTurn = turnCount == 0
        sendJob = scope.launch {
            try {
                val id = sessionId ?: createSession().also { sessionId = it }
                sendQuery(id, text)
                if (isFirstTurn) {
                    turnCount = 1
                    turnModality = modality
                    subscribeLive(id)
                } else {
                    haptics.settle() // §6.13: this turn's completion fold is "handed off".
                    onHandoff(id, modality)
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                haptics.error() // §6.13: entering AssistUiState.Error.
                setState(AssistUiState.Error(reason = e.message ?: "Couldn't send that message", retryText = text))
            }
        }
    }

    /** The FIRST turn's live fold (pre-v4 pattern recovered for this spec): mirrors
     * [com.mewbo.aura.ui.chat.ChatViewModel.subscribeLive]'s own shape (fold every event through
     * [TranscriptReducer], feed the accumulating assistant message through [speech], close on
     * `completion`) but drives [AssistUiState.Streaming] instead of `ChatUiState`. */
    private fun subscribeLive(sessionId: String) {
        streamJob?.cancel()
        setState(streamingState(done = false))
        streamJob = scope.launch {
            try {
                liveEvents(sessionId).collect { event ->
                    reducerState = TranscriptReducer.fold(reducerState, event)
                    speech.onAssistantMessage(latestAssistantMessage(), turnModality, speechMuted)
                    // `completion` AND `stream_end` are BOTH terminal - `SessionStreamClient` really
                    // delivers `stream_end` (after `trySend`, before its own termination flag flips),
                    // and it commonly arrives right after `completion`; the done-already guard makes
                    // that pair idempotent (one `finishStreaming()`/`settle` haptic, not two).
                    val terminal = event is SessionEvent.Completion || event is SessionEvent.StreamEnd
                    // `done` is STICKY: once a terminal event has finalized the card, NEITHER branch
                    // may reset it. Without the guard on the `!terminal` branch too, any non-terminal
                    // event arriving in the gap between `completion` and `stream_end` (a stray delta,
                    // a late tool event) would fold in and reset `done` back to false - re-arming the
                    // overlay composer and double-firing `finishStreaming()`/`settle` on the trailing
                    // `stream_end`.
                    val alreadyDone = (_state.value as? AssistUiState.Streaming)?.done == true
                    when {
                        terminal && !alreadyDone -> finishStreaming()
                        !terminal && !alreadyDone -> setState(streamingState(done = false))
                    }
                }
                // The upstream flow completed on its own (e.g. `stream_end` with no prior
                // `completion` event) - the definitive "nothing more is ever coming" fallback,
                // mirroring `ChatViewModel.subscribeLive`'s own end-of-collect handling.
                if ((_state.value as? AssistUiState.Streaming)?.done == false) finishStreaming()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                haptics.error() // §6.13: entering AssistUiState.Error.
                setState(AssistUiState.Error(reason = e.message ?: "Lost connection to the run", retryText = ""))
            }
        }
    }

    /** Only ever called from within [subscribeLive] - its own job owns [_state] exclusively while
     * active (a [stopStreaming]/[dismiss] call cancels [streamJob] first, so this never races a
     * state the user has already navigated away from), so [streamingState] always has a genuinely
     * current [reducerState] to read here - no need to guard against a stale state kind. */
    private fun finishStreaming() {
        haptics.settle() // §6.13: the turn's natural completion fold.
        setState(streamingState(done = true))
    }

    private fun streamingState(done: Boolean): AssistUiState.Streaming = AssistUiState.Streaming(
        items = reducerState.chatItems,
        done = done,
        speaking = if (done) false else speech.speakingKey.value != null,
    )

    private fun latestAssistantMessage(): ChatItem.AssistantMessage? =
        reducerState.chatItems.lastOrNull { it is ChatItem.AssistantMessage } as? ChatItem.AssistantMessage

    private fun readyState(): AssistUiState.Ready {
        val recent = recentSessionOrNull()
        val title = recent?.title?.takeIf { it.isNotBlank() } ?: recent?.let { "Untitled session" }
        return AssistUiState.Ready(lastSessionTitle = title)
    }

    private fun recentSessionOrNull(): SessionSummary? {
        val cutoff = Instant.now().minus(Duration.ofHours(24))
        return sessions.value
            .mapNotNull { summary -> Timestamps.parseInstantOrNull(summary.updatedAt)?.let { it to summary } }
            .filter { (ts, _) -> ts.isAfter(cutoff) }
            .maxByOrNull { (ts, _) -> ts }
            ?.second
    }

    private fun triggerSessionsRefreshOnce() {
        if (sessionsRefreshTriggered) return
        sessionsRefreshTriggered = true
        scope.launch {
            runCatching { refreshSessions() }
        }
    }

    private fun resetSilenceTimer() {
        silenceJob?.cancel()
        silenceJob = scope.launch {
            delay(SILENCE_TIMEOUT_MS)
            listenJob?.cancel()
            haptics.listeningEnded() // §6.13/R4: capture ended without an accepted transcript.
            setState(readyState())
        }
    }

    /** Guarded partial update for payload-only changes within LISTENING (rms/partial ticks) - a
     * transform applied to the current [AssistUiState.Listening], a no-op if the state has since
     * moved on (e.g. a late-arriving Rms event after Final already fired). */
    private fun updateListening(transform: (AssistUiState.Listening) -> AssistUiState.Listening) {
        val current = _state.value
        if (current is AssistUiState.Listening) _state.value = transform(current)
    }

    /** The one path for a wholesale state-KIND transition. Haptics (§6.13) are explicit calls at
     * the named moments, not tied to every kind change here - so a hypothetical new state doesn't
     * silently buzz just by existing. */
    private fun setState(next: AssistUiState) {
        _state.value = next
    }

    private companion object {
        const val SILENCE_TIMEOUT_MS = 8_000L
    }
}
