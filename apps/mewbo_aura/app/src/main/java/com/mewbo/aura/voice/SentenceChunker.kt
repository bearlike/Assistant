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
        val start = resumePoint(buffer)
        lastBuffer = buffer
        val remainder = buffer.substring(start)
        val boundaries = findBoundaries(remainder)
        // Commit the cursor even with nothing to emit. A shrinking buffer moves the
        // resume point BACKWARDS, and leaving the old value behind is what stranded
        // the tail: the next push, and flush(), would both resume past the end of
        // the text they were handed and return nothing at all.
        if (boundaries.isEmpty()) {
            consumed = start
            return emptyList()
        }

        val utterances = mutableListOf<Utterance>()
        var localStart = 0
        for (boundary in boundaries) {
            emitSegment(remainder.substring(localStart, boundary))?.let { utterances += it }
            localStart = boundary
        }
        consumed = start + localStart
        return utterances
    }

    /**
     * Where to resume reading [buffer], given what has already been spoken.
     *
     * Position-based while the cursor is still IN RANGE, and that is deliberate:
     * a reconciliation that rewrites text already spoken aloud must not re-speak
     * it, because the listener cannot un-hear the first version. Only when the
     * replacement is shorter than [consumed] is the offset meaningless — it points
     * past the end of the string it is being applied to — and clamping it to the
     * new length is what silently read a corrected ending, or an entire final
     * answer, as already-spoken.
     *
     * In that case alone the cursor moves back to where the two buffers diverge, so
     * a pure truncation (a trimmed trailing space) still resumes at the end and
     * says nothing, while a genuine rewrite speaks only its changed tail. Resetting
     * to zero would read the whole reply out a second time.
     *
     * Cost class: `O(buffer length)`, the same pass [findBoundaries] already makes.
     */
    private fun resumePoint(buffer: String): Int {
        if (consumed <= buffer.length) return consumed
        val shared = minOf(buffer.length, lastBuffer.length)
        var agreed = 0
        while (agreed < shared && buffer[agreed] == lastBuffer[agreed]) agreed++
        return agreed
    }

    /**
     * End-of-message remainder, e.g. a final clause with no trailing punctuation.
     *
     * Reads from [consumed] directly rather than re-deriving a resume point: every
     * path into [lastBuffer] already committed its cursor against that exact
     * string, so the offset is valid here by construction. The `coerceAtMost` is
     * kept only as a bound against an empty buffer.
     */
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
