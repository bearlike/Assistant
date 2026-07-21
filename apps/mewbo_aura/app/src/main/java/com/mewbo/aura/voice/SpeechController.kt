package com.mewbo.aura.voice

import com.mewbo.aura.data.model.ChatItem
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Turns transcript text into utterances for [com.mewbo.aura.ui.chat.ChatViewModel]'s two speech
 * affordances (speak-along, P4 read-aloud fidelity) - both share [SentenceChunker]'s
 * markdown-stripping and sentence-chunking instead of one speaking raw text and the other chunked
 * (the P4 bug this closes). [speakingKey] is the single source of truth for "is anything speaking
 * right now" - [com.mewbo.aura.ui.chat.ChatViewModel] mirrors it straight into
 * `ChatUiState.speakingKey`, which already gates both the per-message read-aloud glyph and the
 * sticky stop control (ui/CLAUDE.md). Moved here from `ui/chat` because this package's
 * [AssistTurnMachine] is the SECOND call site (the overlay's in-card first-turn speak-along) - same
 * class, no fork; "all speech in `voice/`" (this package's own CLAUDE.md).
 *
 * [onAssistantMessage] owns the P3 incremental state ([chunker]/[activeKey]/[closedKey]) across
 * repeated calls, one per transcript fold, for the ONE live voice-modality turn; [speakFinalized]
 * is P4's one-shot path for an already-complete message and never touches that state - the two
 * speech sources never contend for the same [SentenceChunker] instance (a manual read-aloud tap
 * during a live voice turn is a [bargeIn] like any other, per its own KDoc).
 */
internal class SpeechController(
    private val synthesizer: Synthesizer,
    scope: CoroutineScope,
) {
    private val _speakingKey = MutableStateFlow<String?>(null)
    val speakingKey: StateFlow<String?> = _speakingKey.asStateFlow()

    private var chunker: SentenceChunker? = null
    private var activeKey: String? = null

    /** The most recently CLOSED (finalized-or-barged-in) key - a still-streaming old turn's later
     * deltas for this SAME key must never resume speech once it's been flushed or discarded. */
    private var closedKey: String? = null

    /** The last utterance id actually enqueued to [synthesizer], across BOTH [onAssistantMessage]
     * and [speakFinalized] - at most one speech source is ever live at a time ([bargeIn] always
     * discards whatever came before a new one starts), so a single field suffices. */
    private var lastEnqueuedId: String? = null

    /** Whether [lastEnqueuedId]'s `Done`/`Error` has already fired - tracked independent of
     * [moreComing] so a completion that arrives WHILE [moreComing] is still `true` (the next
     * transcript fold hasn't run yet) isn't lost. Without this, the mirror-image of the flicker
     * guard below is a real race: [onSynthEvent] correctly ignores that early completion (per
     * [moreComing]'s own guard), but if NO further utterance ever gets enqueued for this turn (the
     * fold that flips [moreComing] to `false` had nothing new left to speak), no future `SynthEvent`
     * will ever arrive to clear [speakingKey] - it would stay stuck non-null forever. Reset to
     * `false` on every fresh [enqueue] and by [bargeIn]. */
    private var lastEnqueuedDone: Boolean = false

    /** `true` while [onAssistantMessage]'s current turn may still enqueue MORE utterances for
     * [activeKey] - [speakingKey] must not clear on [lastEnqueuedId]'s `Done` while this is still
     * true, or a multi-sentence reply would flicker the speaking indicator off between sentences
     * (`Done` for utterance N arrives well before utterance N+1 is even enqueued, since playback is
     * strictly serial). Always `false` for [speakFinalized]'s one-shot case. */
    private var moreComing: Boolean = false

    init {
        scope.launch { synthesizer.events().collect(::onSynthEvent) }
    }

    /** [speakingKey] is set eagerly at enqueue time (see [enqueue]), not on [SynthEvent.Started] -
     * only `Done`/`Error` matter here, to clear it once the LAST enqueued utterance for the current
     * turn finishes and nothing more is coming (see [moreComing]'s own KDoc). [lastEnqueuedDone] is
     * latched regardless of [moreComing] - see its own KDoc for why. */
    private fun onSynthEvent(event: SynthEvent) {
        val id = when (event) {
            is SynthEvent.Started -> return
            is SynthEvent.Done -> event.id
            is SynthEvent.Error -> event.id
        }
        if (id != lastEnqueuedId) return
        lastEnqueuedDone = true
        if (!moreComing) _speakingKey.value = null
    }

    /**
     * Called after every transcript fold with the transcript's current LAST [ChatItem.AssistantMessage]
     * (or `null`). A Text-modality turn or a muted conversation never instantiates a chunker at all
     * ("text turns stay completely silent"). [ChatItem.AssistantMessage.key] is the
     * SAME `assistant:$ts` key for the whole turn (`TranscriptReducer`); the turn closes the moment
     * [ChatItem.AssistantMessage.isStreaming] flips `false`, which flushes the trailing remainder and
     * closes the key for good via [closedKey].
     */
    fun onAssistantMessage(item: ChatItem.AssistantMessage?, modality: InputModality, muted: Boolean) {
        if (item == null || modality != InputModality.Voice || muted || item.key == closedKey) return
        val open = chunker?.takeIf { activeKey == item.key } ?: SentenceChunker(item.key).also {
            chunker = it
            activeKey = item.key
        }
        moreComing = item.isStreaming
        enqueue(open.push(item.text))
        if (!item.isStreaming) {
            enqueue(listOfNotNull(open.flush()))
            closedKey = item.key
            chunker = null
            activeKey = null
            // moreComing just flipped false above, in THIS call - if lastEnqueuedId's completion
            // already fired earlier (while moreComing was still true, see lastEnqueuedDone's KDoc),
            // no future SynthEvent will ever arrive to clear speakingKey; check here instead of only
            // reacting to new events.
            if (lastEnqueuedDone) _speakingKey.value = null
        }
    }

    /**
     * (cross-instance handoff replay): call ONCE, immediately after
     * a fresh binding's history replay and before any live delta resumes
     * ([com.mewbo.aura.ui.chat.ChatViewModel.bind]) - marks whatever text [item] already carries as
     * ALREADY SPOKEN, using the exact same "consumed" cursor [onAssistantMessage]'s own chunker
     * relies on ([SentenceChunker.push]'s own contract: "already-spoken text is NEVER re-spoken").
     * This exists because this app has TWO independent [SpeechController] instances wrapping the
     * SAME synthesizer (the assist overlay's [AssistTurnMachine] and this chat screen's own) - they
     * never contend for the same LIVE turn (voice/CLAUDE.md's own note on that), but a handoff's
     * history replay can still hand a BRAND-NEW instance a message some OTHER instance already
     * spoke, with no way for this one to know that on its own.
     *
     * Utterances produced by the priming push/flush are DISCARDED, never enqueued to [synthesizer] -
     * this only advances the consumed cursor, it never makes a sound. A still-streaming [item]
     * (`isStreaming = true`, e.g. a mid-turn Expand tap on the overlay's first turn) leaves the
     * chunker OPEN under the same key so [onAssistantMessage]'s next real fold - once the live
     * stream resumes delivering genuinely new deltas for that same message - speaks only the
     * unspoken remainder; a completed [item] closes the key outright (mirrors
     * [onAssistantMessage]'s own close-on-finish branch), so a later fold for the SAME key (there
     * shouldn't be one - history is never re-folded - but this stays defensive) can never reopen it.
     * A no-op under the exact same gating [onAssistantMessage] itself uses (`null`/non-Voice/
     * already-closed) - safe to call on EVERY [com.mewbo.aura.ui.chat.ChatViewModel.bind], not just
     * a handoff one.
     */
    fun primeAlreadySpoken(item: ChatItem.AssistantMessage?, modality: InputModality) {
        if (item == null || modality != InputModality.Voice || item.key == closedKey) return
        val primer = SentenceChunker(item.key)
        primer.push(item.text) // discarded - only advances the consumed cursor
        if (item.isStreaming) {
            chunker = primer
            activeKey = item.key
        } else {
            primer.flush() // discarded - consumes any trailing remainder too
            closedKey = item.key
        }
    }

    /** P4 read-aloud: one-shot chunk-and-speak of an already-complete message. Independent of the
     * P3 state above by construction - a local, throwaway [SentenceChunker] instance. */
    fun speakFinalized(messageId: String, text: String) {
        moreComing = false
        val oneShot = SentenceChunker(messageId)
        enqueue(oneShot.push(text))
        enqueue(listOfNotNull(oneShot.flush()))
    }

    /** Barge-in (<=200ms, idempotent): stops whatever's speaking and discards the
     * in-flight turn's chunker for good - see [closedKey]. Safe to call with nothing active. */
    fun bargeIn() {
        synthesizer.stop()
        activeKey?.let { closedKey = it }
        chunker = null
        activeKey = null
        moreComing = false
        lastEnqueuedId = null
        lastEnqueuedDone = false
        _speakingKey.value = null
    }

    private fun enqueue(utterances: List<Utterance>) {
        if (utterances.isEmpty()) return
        _speakingKey.value = utterances.first().id.substringBeforeLast(":")
        utterances.forEach { synthesizer.speak(it.id, it.text) }
        lastEnqueuedId = utterances.last().id
        lastEnqueuedDone = false
    }
}
