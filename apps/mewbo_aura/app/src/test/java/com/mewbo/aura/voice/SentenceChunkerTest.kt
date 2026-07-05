package com.mewbo.aura.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SentenceChunkerTest {

    @Test
    fun `progressive buffer growth emits each sentence once with stable ids`() {
        val chunker = SentenceChunker("msg1")

        assertEquals(listOf(Utterance("msg1:0", "Hello world.")), chunker.push("Hello world. "))
        assertEquals(
            listOf(Utterance("msg1:1", "How are you?")),
            chunker.push("Hello world. How are you? "),
        )
        assertEquals(
            listOf(Utterance("msg1:2", "I'm fine.")),
            chunker.push("Hello world. How are you? I'm fine."),
        )
    }

    @Test
    fun `abbreviations and decimals do not trigger a sentence boundary`() {
        val chunker = SentenceChunker("msg1")
        val result = chunker.push("Dr. Smith weighs 3.5 kg today.")
        assertEquals(listOf(Utterance("msg1:0", "Dr. Smith weighs 3.5 kg today.")), result)
    }

    @Test
    fun `a closed code fence collapses to one code-block-omitted utterance`() {
        val chunker = SentenceChunker("msg1")
        val buffer = "Here is code:\n```\nval x = 1\nprintln(x)\n```\n"
        val result = chunker.push(buffer)
        assertEquals(
            listOf(
                Utterance("msg1:0", "Here is code:"),
                Utterance("msg1:1", "Code block omitted."),
            ),
            result,
        )
    }

    @Test
    fun `reconcile that rewrites already-emitted text does not re-emit`() {
        val chunker = SentenceChunker("msg1")

        // Both strings are 13 chars: proves the skip is position-based, not content-based.
        assertEquals(listOf(Utterance("msg1:0", "Hello world.")), chunker.push("Hello world. "))
        assertTrue(chunker.push("Hello there. ").isEmpty())
    }

    @Test
    fun `reconcile correcting the unspoken remainder emits the corrected text`() {
        val chunker = SentenceChunker("msg1")

        assertEquals(listOf(Utterance("msg1:0", "Hello world.")), chunker.push("Hello world. Wai"))
        assertEquals(
            listOf(Utterance("msg1:1", "Actually nevermind.")),
            chunker.push("Hello world. Actually nevermind."),
        )
    }

    @Test
    fun `flush emits the trailing remainder once streaming ends`() {
        val chunker = SentenceChunker("msg1")
        chunker.push("Hello world. Almost done")

        assertEquals(Utterance("msg1:1", "Almost done"), chunker.flush())
        assertNull(chunker.flush())
    }

    @Test
    fun `markdown stripping normalizes links, inline code, and emphasis to plain text`() {
        val input = "Check **this** [amazing link](https://example.com) and `code` and _emphasis_ now."
        assertEquals(
            "Check this amazing link and code and emphasis now.",
            SentenceChunker.stripMarkdown(input),
        )
    }
}
