package com.mewbo.aura.ui.chat

import android.net.Uri
import androidx.compose.runtime.Immutable
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.ComposerScope
import com.mewbo.aura.data.model.ModelCatalog
import com.mewbo.aura.data.model.StagedAttachment
import com.mewbo.aura.voice.InputModality
import com.mewbo.aura.voice.TranscriberError

/** Coarse run status driving the composer affordance (Send vs. Stop) and the thinking indicator. */
enum class RunPhase { Idle, Sending, Streaming, Done, Error }

/**
 * A turn is actively executing on the backend RIGHT NOW (the same {Sending, Streaming} predicate the
 * liveness cues read off `runPhase` — `ChatScreen.overWash`, `ChatTranscript.isRunLive`,
 * `ChatSurface.isRunning`). This is the ComposerOptionsSheet scope-freeze gate (Gitea #185 P5): the
 * Project/Tools picks are re-resolved from every `/query`'s own request body, so mutating them WHILE
 * a run is in flight would desync the live turn — but Idle/Done/Error carry no live run, so scope is
 * freely editable for the NEXT turn, including on an already-created, idle session.
 */
val RunPhase.isRunInFlight: Boolean
    get() = this == RunPhase.Sending || this == RunPhase.Streaming

/**
 * Composer mic state (Gitea #180 P2) - a UI-facing projection of the injected
 * [com.mewbo.aura.voice.Transcriber]'s raw event stream ([DictationDecision.next] is the pure
 * mapping), driving [com.mewbo.aura.ui.composer.ComposerState.Dictation]'s existing waveform/
 * partial-text center content. NEVER the source of truth for the composer's real editable draft -
 * that stays [ChatSurface]'s own local `draft` (same "not persisted across process death" posture
 * as [ChatUiState.activeTurnModality]). [Final] is a one-shot signal [ChatSurface] observes once
 * (a `LaunchedEffect`) to fill that draft and re-tag the pending send [InputModality.Voice], then
 * hands back to [ChatViewModel.consumeDictationFinal] so it doesn't replay on the next
 * recomposition/rotation. Also backs [ChatViewModel.stopDictation]'s "keep whatever partial
 * landed" semantics - reusing [Final] for both a genuine recognizer Final and a manual stop is
 * deliberate: from [ChatSurface]'s POV both mean "dictation is over, put this text in the field."
 */
sealed interface DictationState {
    data object Idle : DictationState
    data class Listening(val partial: String? = null, val rmsDb: Float = 0f) : DictationState
    data class Final(val text: String) : DictationState
}

/**
 * Chat screen state - host-agnostic (consumed by both [ChatScreen] and, later, the assist-overlay
 * host; apps/mewbo_aura/CLAUDE.md "one chat composable tree, two hosts"). [items] is always the
 * direct output of [com.mewbo.aura.data.model.TranscriptReducer], plus any purely client-side
 * failures that never became a [com.mewbo.aura.data.model.SessionEvent].
 *
 * `@Immutable`: [ChatViewModel] only ever hands out a freshly-copied instance (`_state.update {
 * it.copy(...) }`), never mutates one after emitting it - honest to assert, and needed since
 * `List<ChatItem>` alone isn't enough for the compiler to infer stability (Task E review, fix
 * round 1).
 */
@Immutable
data class ChatUiState(
    val title: String? = null,
    val items: List<ChatItem> = emptyList(),
    val runPhase: RunPhase = RunPhase.Idle,
    val isOffline: Boolean = false,
    val isLoadingHistory: Boolean = false,
    /**
     * Spec §6.12: a non-recoverable completion disables the composer and drops the ErrorCard's
     * Retry button. **v1 always leaves this `false`** - `CompletionPayload` doesn't carry the
     * backend's actual `recoverable` signal (that field only exists on the separate
     * `summarize_session()` REST view, computed as `status == "completed"`, i.e. a session that
     * finished ITS WORK successfully - the opposite of what "ended, stop trying" would mean for a
     * live open chat, where a normal successful turn obviously should not block the next message).
     * Wired end-to-end (composer disables, placeholder shows) so a future real signal is a one-line
     * flip, not a new code path; flagged in the task report.
     */
    val sessionEnded: Boolean = false,
    /** Display name for the greeting (§6.6) - passthrough of `SettingsStore.displayName`, empty
     * when unset (greeting falls back to the no-name copy). */
    val displayName: String = "",
    /** The [ChatItem.AssistantMessage.key] currently being read aloud via the injected
     * [com.mewbo.aura.voice.Synthesizer], or `null` when nothing is speaking - drives the action
     * row's active read-aloud state (spec §6.5). */
    val speakingKey: String? = null,
    /** The currently-bound session id (`null` = fresh/unsaved chat) - passthrough of
     * [ChatViewModel]'s own `SessionBinding.currentId`. Exists so [ChatTranscript] can key its
     * M7 chip-entrance "already seen" tracking on the REAL rebind signal rather than its own
     * composition lifetime, which outlives session switches (fix-round-3 minor #5: chip entrances
     * were replaying on every session switch because that tracking set was seeded once at first
     * mount and never reset when `bind()` loaded a different session's history in place). */
    val sessionId: String? = null,
    /** The user's persisted model choice (bare id), or `null` to mean "use the API default" -
     * passthrough of `SettingsStore.selectedModel` with blank mapped to `null`. */
    val selectedModel: String? = null,
    /** Model picker data, loaded lazily on the sheet's first open ([ChatViewModel.loadModelsIfNeeded])
     * rather than eagerly at [ChatViewModel.bind] - `null` means "not loaded yet" (top bar falls
     * back to "Core"), not "offline forever". */
    val models: ModelCatalog? = null,
    /** Composer "+" sheet's project/MCP-tool scoping for the next fresh-turn send (Gitea #177 W2) -
     * catalogs load lazily on the sheet's first open ([ChatViewModel.refreshComposerScope]). */
    val composerScope: ComposerScope = ComposerScope(),
    /** Files picked in the options sheet but not yet uploaded - rendered as chips above the input
     * (Gitea #177 W2), cleared on a successful fresh-turn send. */
    val stagedAttachments: List<StagedAttachment> = emptyList(),
    /** `true` while [stagedAttachments] are being uploaded ahead of a `/query` call - the composer's
     * own Sending-phase stop-tile affordance already covers the visible "in progress" state (runPhase
     * flips before the upload starts); this flag exists for the options sheet to gate on. */
    val isUploadingAttachments: Boolean = false,
    /** Which input channel started the current (or most recently started) turn - a client-only hint
     * (Gitea #180 P1) so a later phase can e.g. speak a voice-initiated reply aloud; never reaches
     * the backend. Set by [ChatViewModel.send]'s `modality` param for a fresh turn opened in this
     * app, or by [ChatViewModel.bind]'s `handoffModality` when picking up an assist-overlay handoff
     * that was already streaming before this screen existed - a steer into an already-in-flight run
     * never overwrites it (see [SendDecision.modalityForSend]: only the turn that actually opened the
     * run tags it). Defaults to [InputModality.Text] and is not persisted across process death. */
    val activeTurnModality: InputModality = InputModality.Text,
    /** See [DictationState]'s own KDoc (Gitea #180 P2). */
    val dictation: DictationState = DictationState.Idle,
    /** Pull-up handoff draft (user directive 2026-07-04): text the user had typed in the assist
     * overlay's pill when they swiped it up into the app, awaiting one-shot application to the
     * composer field. Applied by `ChatSurface` exactly like a [DictationState.Final] EXCEPT it
     * never tags the pending send [InputModality.Voice] — the text was typed, and a masqueraded
     * Voice tag would trigger speak-along on the reply. Acked via
     * `ChatCallbacks.onHandoffDraftConsumed` → [ChatViewModel.consumeHandoffDraft]. */
    val pendingHandoffDraft: String? = null,
    /** Latches `false` the first time the injected `Transcriber` reports
     * [TranscriberError.Unavailable] (no on-device recognizer - the real state on redroid with the
     * platform impl; the debug `FakeTranscriber` never reports it) - gates the composer's mic
     * affordance disabled from then on (Gitea #180 P2 ruling), since the device's STT capability
     * isn't going to change mid-process. */
    val dictationAvailable: Boolean = true,
    /** Per-conversation speak-along mute (Gitea #180 P3, #175 §7.1) - latched by
     * [ChatViewModel.stopSpeaking] when the sticky stop control interrupts THIS turn's own live
     * voice-modality speech ([SendDecision.shouldMuteOnStopSpeaking]); reset by the next voice-
     * initiated [ChatViewModel.send] (fresh turn = fresh consent to speak) or by [ChatViewModel.bind]
     * (a session switch starts over). Not persisted, same posture as [activeTurnModality]. */
    val speechMuted: Boolean = false,
)

/**
 * Callbacks a host wires into [ChatSurface]; kept as one bundle so the surface signature stays
 * small. [onNotice]/[onReadAloudToggle] default to no-ops so the pre-existing overlay host
 * (`ui/overlay/AssistOverlayScreen.kt`, out of this task's file ownership) keeps compiling
 * unchanged until W3 wires real behavior for its own host.
 */
data class ChatCallbacks(
    /** [InputModality] (Gitea #180 P2) is `ChatSurface`'s own locally-tracked `pendingModality` -
     * [InputModality.Voice] once a dictation Final/stop has landed text in the field, reset to
     * [InputModality.Text] by any subsequent real keystroke. */
    val onSend: (String, InputModality) -> Unit,
    val onStop: () -> Unit,
    val onRetry: () -> Unit,
    /** Start dictation (composer C1's mic glyph). Nullable (not a no-op default): `ui/overlay`'s
     * `AssistOverlayScreen` (out of this task's ownership) conditionally supplies this only in
     * specific states - keep it that shape rather than forcing a non-null default that would break
     * that call site. */
    val onMicTap: (() -> Unit)? = null,
    /** End dictation without sending (composer C3's stop tile) - a DISTINCT action from starting
     * it, previously (wrongly) collapsed onto [onMicTap] in `ChatSurface`'s wiring (W1-C review
     * deferred-Minor). Nullable for the same reason as [onMicTap] - no host wires either yet. */
    val onDictationStop: (() -> Unit)? = null,
    /** A full voice turn (composer C1's rounded-square tile) - this app's existing
     * `AssistTurnMachine.startListening()` voice-capture path, which tags the resulting turn
     * `InputModality.Voice` (Gitea #180 P1) via its private `beginTurn`, overlay-scoped. Nullable so
     * a host that genuinely can't reach that seam (the in-chat host, today) falls through to a KISS
     * notice in `ChatSurface` instead of rebuilding a second turn machine (brief: "do NOT... KISS"). */
    val onVoiceModeTap: (() -> Unit)? = null,
    /** Spec §6.9 toast surface - the host (another task, ui/navigation) owns the actual
     * `NoticeController`; this is the seam. */
    val onNotice: (String) -> Unit = {},
    /** Tap the action row's read-aloud glyph on this specific assistant message - toggles
     * speak/stop via the injected `Synthesizer` (spec §6.5). */
    val onReadAloudToggle: (ChatItem.AssistantMessage) -> Unit = {},
    /** Tap the composer's leading "+" - opens the options sheet (Gitea #177 W2). Default no-op so
     * the pre-existing overlay host keeps compiling unchanged, same convention as [onMicTap]. */
    val onAttachTap: () -> Unit = {},
    /** Tap the pre-session scope indicator row (Gitea #178 W1-B) - opens the SAME options sheet as
     * [onAttachTap]; a distinct field (rather than reusing it) so a host can tell the two tap
     * origins apart later. Default no-op, same convention as [onAttachTap]. */
    val onScopeIndicatorTap: () -> Unit = {},
    /** Remove one staged attachment chip before it's sent. */
    val onRemoveAttachment: (Uri) -> Unit = {},
    /** [ChatSurface]'s `LaunchedEffect` ack once it's applied a [DictationState.Final] to the
     * draft/pending-modality (Gitea #180 P2) - see [ChatViewModel.consumeDictationFinal]. */
    val onDictationFinalConsumed: () -> Unit = {},

    /** One-shot ack once `ChatSurface` has applied a [ChatUiState.pendingHandoffDraft] to the
     * composer field — same replay-safety shape as [onDictationFinalConsumed]. */
    val onHandoffDraftConsumed: () -> Unit = {},
)
