package com.mewbo.aura.ui.common

/**
 * Defensive closer applied at the streaming -> finalized swap (ui/CLAUDE.md "apply the
 * unclosed-markdown-buffer guard while streaming so half-open fences don't explode layout").
 * Not a parser - just guarantees a truncated buffer (e.g. `stream_end` closing streaming state
 * before an authoritative `assistant` finalize arrived - data/CLAUDE.md) can never leave a fenced
 * code block open, since an unterminated fence would otherwise swallow every line after it into
 * one unbounded code block when handed to the real markdown renderer.
 */
object MarkdownBuffer {

    fun sanitize(partial: String): String {
        val fenced = closeFenceIfOdd(partial)

        val withoutFences = fenced.replace(FENCE_MARKER, "")
        val bolded = closeIfOdd(fenced, BOLD_MARKER, count(withoutFences, BOLD_MARKER))

        val withoutBoldOrFences = withoutFences.replace(BOLD_MARKER, "")
        val italicOpen = countItalicCandidates(withoutBoldOrFences) % 2 == 1
        return if (italicOpen) bolded + "*" else bolded
    }

    /**
     * Single `*` occurrences, excluding line-start list markers (CommonMark `* item` bullets) -
     * a streaming buffer that happens to close mid-list on an odd number of bullet lines would
     * otherwise be mistaken for an unclosed italic run and get a stray `*` appended.
     */
    private fun countItalicCandidates(text: String): Int =
        text.lineSequence().sumOf { line -> LIST_MARKER.replaceFirst(line, "").count { it == '*' } }

    /** A fence marker only closes a code block when it starts its own line - a bare `text + "```"` would not. */
    private fun closeFenceIfOdd(text: String): String {
        if (count(text, FENCE_MARKER) % 2 == 0) return text
        val separator = if (text.endsWith("\n")) "" else "\n"
        return text + separator + FENCE_MARKER
    }

    private fun count(text: String, marker: String): Int {
        var occurrences = 0
        var index = text.indexOf(marker)
        while (index >= 0) {
            occurrences++
            index = text.indexOf(marker, index + marker.length)
        }
        return occurrences
    }

    private fun closeIfOdd(text: String, marker: String, occurrences: Int): String =
        if (occurrences % 2 == 1) text + marker else text

    private const val FENCE_MARKER = "```"
    private const val BOLD_MARKER = "**"
    /** A `*` (optionally indented up to 3 spaces/tabs, CommonMark's limit) starting a bullet line. */
    private val LIST_MARKER = Regex("^[ \t]{0,3}\\*(?=[ \t]|$)")
}
