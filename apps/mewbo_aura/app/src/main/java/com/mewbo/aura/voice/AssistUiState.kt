package com.mewbo.aura.voice

import androidx.compose.runtime.Immutable
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.orb.OrbState

/**
 * Voice-turn / overlay state machine states (v5: voice-first overlay - auto-listen on
 * trigger, the first response streams IN the overlay as a reference-style card, the SECOND
 * interaction onward hands off to the app - supersedes v4's text-first, handoff-only
 * contract). Exhaustive `when` everywhere - no `else` branch, so a hypothetical new state fails
 * compilation instead of silently losing a UI mapping (same discipline as [OrbState]).
 */
@Immutable
sealed interface AssistUiState {

    /** Pre-show / post-teardown sentinel - the machine's construction-time default and the target
     * of [AssistTurnMachine.dismiss]. The overlay is never actually on screen in this state (see
     * [com.mewbo.aura.ui.overlay.AssistOverlayScreen]'s entrance/exit `AnimatedVisibility`, keyed
     * off `!is Idle`) - [AssistTurnMachine.show] is what the overlay host calls to leave it. */
    data object Idle : AssistUiState

    /**
     * The overlay's resting state ([AssistTurnMachine.show], fired from `AuraSession.onShow()`):
     * visible, no turn started yet. On a real invocation this is momentary - `AuraSession.onShow()`
     * immediately follows with [AssistTurnMachine.startListening] when `RECORD_AUDIO` is granted
     * (auto-listen on trigger); this is the landing state for a denied-grant cold start
     * and for hosts (the debug preview activity) that opt out of auto-listen. [lastSessionTitle]
     * backs `ContinueLastSessionChip` - a session updated within the last 24h, refreshed once via
     * [AssistTurnMachine.refreshSessions] (best-effort; absent on a cold start before that
     * resolves).
     */
    data class Ready(val lastSessionTitle: String?) : AssistUiState

    /**
     * The mic-hot capture path - auto-entered from [Ready] on a granted trigger, or
     * reachable via an explicit mic tap same as before. Cancellable back to [Ready] via the
     * composer's stop tile. A `Final` transcript here reaches the exact same
     * [AssistTurnMachine.beginTurn] path a typed send does - the machine doesn't care how the text
     * arrived.
     */
    data class Listening(val partial: String, val rmsDb: Float) : AssistUiState

    /** Ensure-session + dispatch-query in flight, ahead of either [Streaming] (the FIRST turn) or
     * [AssistTurnMachine]'s `onHandoff` callback (every turn after) - composer disabled, orb docks
     * into the pill (D-1). */
    data object Sending : AssistUiState

    /**
     * The FIRST turn's response, rendered IN the overlay as a card ("overlay card
     * pattern" - supersedes v4's handoff-only contract). [items] folds
     * `RunRepository.live(sessionId)` through the SAME [com.mewbo.aura.data.model.TranscriptReducer]
     * the main chat surface uses (the pre-v4 machine's pattern, recovered from git history for
     * never a fork), so the card reuses the shared `ChatTranscript` composable. [done] flips
     * `true` once the run's `completion`/`stream_end` closes streaming, or the user taps the card's
     * stop control ([AssistTurnMachine.stopStreaming] - client-side detach only, the backend run
     * keeps going either way - v1 semantics, unchanged). [speaking] mirrors whether the voice
     * speak-along (`SpeechController`, moved from `ui/chat`) is actively playing -
     * forced `false` the moment [done] flips `true`; the machine doesn't chase the synthesizer's
     * own trailing-utterance tail once the transcript itself is finalized.
     */
    data class Streaming(val items: List<ChatItem>, val done: Boolean, val speaking: Boolean) : AssistUiState

    /** A client-side failure (session-create, a fresh-turn dispatch, or a lost live connection) -
     * no handoff fires. [reason] renders inline near the composer (§6.12 "quiet error"); [retryText]
     * is restored into the composer's draft so the user can edit-and-resend through the same send
     * affordance any other turn uses - blank when there's nothing queued to resend (a lost
     * connection after an already-successful dispatch, or the mic-permission notice). */
    data class Error(val reason: String, val retryText: String) : AssistUiState
}

/** §8.1 orb-state mapping. Position/size (centered-large vs. docked-small) is an overlay-layout
 * concern, not an [OrbState] concern - see [com.mewbo.aura.ui.overlay.AssistOverlayScreen]. */
fun AssistUiState.toOrbState(): OrbState = when (this) {
    AssistUiState.Idle -> OrbState.Idle
    is AssistUiState.Ready -> OrbState.Idle
    is AssistUiState.Listening -> OrbState.Listening(rmsDb)
    AssistUiState.Sending -> OrbState.Thinking
    is AssistUiState.Streaming -> if (hasAssistantContent(items)) OrbState.Idle else OrbState.Thinking
    is AssistUiState.Error -> OrbState.Error
}

private fun hasAssistantContent(items: List<ChatItem>): Boolean =
    items.any { it is ChatItem.AssistantMessage && it.text.isNotEmpty() }
