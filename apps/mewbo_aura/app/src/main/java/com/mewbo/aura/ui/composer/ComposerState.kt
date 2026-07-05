package com.mewbo.aura.ui.composer

/**
 * The composer's five visual states (spec §6.2 C1-C5, minus C5 which is a [ComposerStyle] rather
 * than a state - it's a paint variant that can host any of these four). [AuraComposer] is dumb: it
 * only renders whatever [ComposerState] it's handed, never derives one internally. [resolve] is
 * the one place that derivation logic lives, kept pure so mode selection is unit-testable without
 * Compose (module CLAUDE.md atomic-class paradigm: state as data, behavior as methods).
 */
sealed interface ComposerState {
    /** C1: `Ask Mewbo` placeholder, muted mic/waveform trailing circle. */
    data object Idle : ComposerState

    /** C2: user has typed - cursor visible, trailing circle is the accent send arrow. */
    data object Typing : ComposerState

    /**
     * C3: live dictation. [rmsDb] is a pre-normalized 0f..1f amplitude (voice/-layer concern to
     * map real dBFS into this range - out of this task's ownership); [partialText] non-null means
     * the transcript has started finalizing words and replaces the bar render with text (spec
     * §6.2: "transcript may replace bars as words finalize").
     */
    data class Dictation(val rmsDb: Float, val partialText: String? = null) : ComposerState

    /**
     * C4: the assistant is generating a response. Typing stays live (spec: "typing allowed, text
     * buffers"); [hasDraft] alone decides the trailing circle's stop-vs-send glyph, independent of
     * whatever the center content renders.
     */
    data class Streaming(val hasDraft: Boolean) : ComposerState

    companion object {
        /**
         * Pure mode derivation (spec §6.2 table), one input per independent signal the host can be
         * in. Priority - dictation wins over everything (it's an explicit, exclusive user action);
         * an in-flight run wins over plain typing (C4 must render even with an empty draft, to show
         * the stop affordance); otherwise draft text alone decides Typing vs. Idle.
         */
        fun resolve(
            draftText: String,
            isDictating: Boolean,
            isStreaming: Boolean,
            rmsDb: Float = 0f,
            partialText: String? = null,
        ): ComposerState = when {
            isDictating -> Dictation(rmsDb = rmsDb, partialText = partialText)
            isStreaming -> Streaming(hasDraft = draftText.isNotBlank())
            draftText.isNotBlank() -> Typing
            else -> Idle
        }
    }
}

/**
 * Paint variant, orthogonal to [ComposerState] (spec §6.2 C5 + Rev D §D-1). [Docked] is the
 * in-chat composer; [FloatingOverlay] is the assist-overlay's floating pill - same states, same
 * anatomy, different fill (and, per Rev D-1, the caller usually supplies [AuraComposer]'s
 * `trailingAccessory` to host the orb instead of the default action circle).
 */
enum class ComposerStyle { Docked, FloatingOverlay }
