package com.mewbo.aura.ui.composer

import org.junit.Assert.assertEquals
import org.junit.Test

class ComposerStateTest {

    @Test
    fun `empty draft, not dictating, not streaming resolves to Idle`() {
        assertEquals(
            ComposerState.Idle,
            ComposerState.resolve(draftText = "", isDictating = false, isStreaming = false),
        )
    }

    @Test
    fun `non-blank draft resolves to Typing when idle`() {
        assertEquals(
            ComposerState.Typing,
            ComposerState.resolve(draftText = "hello", isDictating = false, isStreaming = false),
        )
    }

    @Test
    fun `blank (whitespace-only) draft still resolves to Idle`() {
        assertEquals(
            ComposerState.Idle,
            ComposerState.resolve(draftText = "   ", isDictating = false, isStreaming = false),
        )
    }

    @Test
    fun `dictation wins over streaming and typing`() {
        val state = ComposerState.resolve(
            draftText = "buffered text",
            isDictating = true,
            isStreaming = true,
            rmsDb = 0.6f,
            partialText = "partial words",
        )
        assertEquals(ComposerState.Dictation(rmsDb = 0.6f, partialText = "partial words"), state)
    }

    @Test
    fun `streaming with empty draft resolves to Streaming with hasDraft false`() {
        assertEquals(
            ComposerState.Streaming(hasDraft = false),
            ComposerState.resolve(draftText = "", isDictating = false, isStreaming = true),
        )
    }

    @Test
    fun `streaming with typed draft resolves to Streaming with hasDraft true`() {
        assertEquals(
            ComposerState.Streaming(hasDraft = true),
            ComposerState.resolve(draftText = "still typing", isDictating = false, isStreaming = true),
        )
    }

    @Test
    fun `streaming wins over plain typing even with a non-blank draft`() {
        val state = ComposerState.resolve(draftText = "queued message", isDictating = false, isStreaming = true)
        assert(state is ComposerState.Streaming)
    }
}
