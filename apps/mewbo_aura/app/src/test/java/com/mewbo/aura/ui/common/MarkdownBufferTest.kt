package com.mewbo.aura.ui.common

import org.junit.Assert.assertEquals
import org.junit.Test

class MarkdownBufferTest {

    @Test
    fun `already balanced text is unchanged`() {
        val text = "Here's a **bold** claim with `inline code` and a\n```\nfenced block\n```\ndone."
        assertEquals(text, MarkdownBuffer.sanitize(text))
    }

    @Test
    fun `plain text without any markdown constructs is unchanged`() {
        val text = "Tokyo looks partly cloudy tomorrow with a high near 22C."
        assertEquals(text, MarkdownBuffer.sanitize(text))
    }

    @Test
    fun `unclosed fenced code block gets a closing fence appended`() {
        val truncated = "Here's the fix:\n```kotlin\nfun main() {"
        val result = MarkdownBuffer.sanitize(truncated)
        assertEquals(truncated + "\n```", result)
    }

    @Test
    fun `unclosed bold run gets closed`() {
        val truncated = "This is **very important"
        assertEquals("$truncated**", MarkdownBuffer.sanitize(truncated))
    }

    @Test
    fun `unclosed italic run gets closed`() {
        val truncated = "This is *somewhat important"
        assertEquals("$truncated*", MarkdownBuffer.sanitize(truncated))
    }

    @Test
    fun `closed bold does not get double-counted as an open italic`() {
        val text = "This is **bold** and this is not italic"
        assertEquals(text, MarkdownBuffer.sanitize(text))
    }

    @Test
    fun `unclosed fence and unclosed bold inside it both get closed`() {
        val truncated = "Summary:\n```\n**bold inside a fence"
        val result = MarkdownBuffer.sanitize(truncated)
        // Fence closes first (so the code block terminates), bold is closed within the pre-fence text.
        assertEquals(truncated + "\n```" + "**", result)
    }

    @Test
    fun `empty input is unchanged`() {
        assertEquals("", MarkdownBuffer.sanitize(""))
    }

    @Test
    fun `streaming bullet list with an odd item count is not mistaken for unclosed italic`() {
        val text = "Here are the options:\n* Option one\n* Option two\n* Option three"
        assertEquals(text, MarkdownBuffer.sanitize(text))
    }

    @Test
    fun `bullet list item with a genuine closed italic run is unchanged`() {
        val text = "* Item with *emphasis* inside"
        assertEquals(text, MarkdownBuffer.sanitize(text))
    }

    @Test
    fun `bullet list item with a genuine unclosed italic run still gets closed`() {
        val truncated = "* Item with *unclosed emphasis"
        assertEquals("$truncated*", MarkdownBuffer.sanitize(truncated))
    }
}
