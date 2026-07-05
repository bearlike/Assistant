package com.mewbo.aura.voice

/** A synthesizer-ready chunk of assistant text. */
data class Utterance(val id: String, val text: String)

/**
 * Watches the assistant streaming buffer and emits newly-completed sentences exactly once, even
 * as the buffer grows or is reconciled (replaced) mid-stream. [consumed] tracks a position in
 * the RAW buffer, not the markdown-stripped text, so stripped-length drift (links, code fences)
 * never desyncs "what's already been spoken" from "what's new."
 */
class SentenceChunker(private val messageId: String) {

    private var consumed = 0
    private var nextIndex = 0
    private var lastBuffer = ""

    /** Called with the CURRENT full streaming buffer; returns newly-completed sentences. */
    fun push(buffer: String): List<Utterance> {
        lastBuffer = buffer
        val start = consumed.coerceAtMost(buffer.length)
        val remainder = buffer.substring(start)
        val boundaries = findBoundaries(remainder)
        if (boundaries.isEmpty()) return emptyList()

        val utterances = mutableListOf<Utterance>()
        var localStart = 0
        for (boundary in boundaries) {
            emitSegment(remainder.substring(localStart, boundary))?.let { utterances += it }
            localStart = boundary
        }
        consumed = start + localStart
        return utterances
    }

    /** End-of-message remainder, e.g. a final clause with no trailing punctuation. */
    fun flush(): Utterance? {
        val start = consumed.coerceAtMost(lastBuffer.length)
        val raw = lastBuffer.substring(start)
        consumed = lastBuffer.length
        return emitSegment(raw)
    }

    private fun emitSegment(raw: String): Utterance? {
        val stripped = stripMarkdown(raw)
        if (stripped.isBlank()) return null
        return Utterance(id = "$messageId:$nextIndex", text = stripped).also { nextIndex++ }
    }

    /** Returns split points (indices into [text]) after which a completed sentence ends. */
    private fun findBoundaries(text: String): List<Int> {
        val boundaries = mutableListOf<Int>()
        var i = 0
        var fenceOpen = false
        while (i < text.length) {
            if (text.startsWith(CODE_FENCE_MARKER, i)) {
                i += CODE_FENCE_MARKER.length
                fenceOpen = !fenceOpen
                if (!fenceOpen) boundaries += i
                continue
            }
            if (fenceOpen) {
                i++
                continue
            }
            val c = text[i]
            if (c == '\n') {
                val segStart = boundaries.lastOrNull() ?: 0
                if (text.substring(segStart, i).isNotBlank()) boundaries += i + 1
                i++
                continue
            }
            if ((c == '.' || c == '!' || c == '?') && isSentenceEnd(text, i)) {
                var end = i + 1
                if (end < text.length && text[end] == ' ') end++
                boundaries += end
            }
            i++
        }
        return boundaries
    }

    private fun isSentenceEnd(text: String, punctuationIndex: Int): Boolean {
        val followedByBoundary = punctuationIndex + 1 >= text.length || text[punctuationIndex + 1].isWhitespace()
        return followedByBoundary && !isAbbreviation(text, punctuationIndex)
    }

    private fun isAbbreviation(text: String, periodIndex: Int): Boolean {
        var start = periodIndex
        while (start > 0 && text[start - 1].isLetter()) start--
        val word = text.substring(start, periodIndex).lowercase()
        return word.isNotEmpty() && word in ABBREVIATIONS
    }

    companion object {
        private const val CODE_FENCE_MARKER = "```"
        private const val CODE_BLOCK_PLACEHOLDER = "Code block omitted."

        private val ABBREVIATIONS = setOf(
            "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "approx",
        )
        private val CODE_FENCE_REGEX = Regex("```[\\s\\S]*?```")
        private val LINK_REGEX = Regex("""\[([^\]]*)]\([^)]*\)""")
        private val INLINE_CODE_REGEX = Regex("`([^`]*)`")
        private val EMPHASIS_REGEX = Regex("[*_#>]+")

        /** Strips markdown to speech-ready plain text. Public so callers can pre-clean flush()ed text. */
        fun stripMarkdown(text: String): String {
            var result = CODE_FENCE_REGEX.replace(text) { CODE_BLOCK_PLACEHOLDER }
            result = LINK_REGEX.replace(result) { it.groupValues[1] }
            result = INLINE_CODE_REGEX.replace(result) { it.groupValues[1] }
            result = EMPHASIS_REGEX.replace(result, "")
            return result.trim()
        }
    }
}
