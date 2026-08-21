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
    fun `a reconciliation that SHRINKS the buffer still speaks the new tail`() {
        val chunker = SentenceChunker("msg1")

        // Three sentences spoken, cursor at 34 — past the end of the replacement below.
        assertEquals(3, chunker.push("Alpha one. Beta two. Gamma three. ").size)

        // The server replaces the buffer with a SHORTER correction. Clamping the old
        // cursor to the new length read this tail as already-spoken and returned
        // nothing; the resume point moves back to where the two buffers diverge.
        assertEquals(listOf(Utterance("msg1:3", "Zeta.")), chunker.push("Alpha one. Zeta."))
    }

    @Test
    fun `a shrink with no complete sentence still leaves the tail flushable`() {
        val chunker = SentenceChunker("msg1")
        chunker.push("Alpha one. Beta two. Gamma three. ")

        // No terminator, so push emits nothing — but it must still COMMIT the moved
        // cursor, or flush() resumes past the end and the whole tail is lost.
        assertTrue(chunker.push("Alpha one. Zeta").isEmpty())
        assertEquals(Utterance("msg1:3", "Zeta"), chunker.flush())
    }

    @Test
    fun `truncating the tail does not re-speak the whole reply`() {
        val chunker = SentenceChunker("msg1")

        // Cursor lands at 13 — only the first sentence is spoken, the rest is unsent.
        assertEquals(listOf(Utterance("msg1:0", "Hello world.")), chunker.push("Hello world. Se"))

        // A buffer shorter than the cursor, agreeing on everything already spoken.
        // A reset-to-zero cure would re-speak "Hello world." here, which is a worse
        // defect than the one being fixed: the listener hears the reply twice.
        assertTrue(chunker.push("Hello world").isEmpty())
        assertNull(chunker.flush())
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
