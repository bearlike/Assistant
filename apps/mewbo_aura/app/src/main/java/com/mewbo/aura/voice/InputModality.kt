package com.mewbo.aura.voice

/**
 * Client-only tag for which input channel started a chat turn - typed vs. voice ('s
 * modality seam). Purely a UI hint for later phases (e.g. speaking a voice-initiated reply aloud);
 * NEVER put on the wire - the backend contract has no concept of it and is untouched by this.
 * Not persisted: a process death mid-turn resets to [Text] on the next cold start/rebind, an
 * accepted v1 limitation.
 */
enum class InputModality {
    Voice,
    Text,
    ;

    companion object {
        /**
         * Parses the assist-overlay handoff's plain-string extra (an [InputModality.name] value,
         * e.g. "Voice"/"Text") back into a real [InputModality] - defaults to [Text] for anything
         * null/unrecognized, never throws. [ChatViewModel][com.mewbo.aura.ui.chat.ChatViewModel]'s
         * `bind()` is the one caller; everything upstream of it (`MainActivity`, `AuraNavHost`,
         * `ChatScreen`) shuttles the raw `String?` so those `ui/` layers never need to import
         * `voice/` (apps/mewbo_aura/CLAUDE.md package layering).
         */
        fun fromExtra(value: String?): InputModality = entries.firstOrNull { it.name == value } ?: Text
    }
}
