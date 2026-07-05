package com.mewbo.aura.ui.composer

import android.net.Uri
import com.mewbo.aura.data.model.StagedAttachment
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mockito.Mockito.mock

/**
 * Pure add/remove/clear-on-send logic for the composer's staged-attachments chip row - see
 * [StagedAttachmentsReducer]'s own doc for why this is split out of [com.mewbo.aura.ui.chat.ChatViewModel].
 *
 * `android.net.Uri`'s real methods throw ("not mocked") under this module's plain-JVM `android.jar`
 * stub (no Robolectric here) - [mock] sidesteps that entirely (Mockito's inline mock maker
 * instruments the class rather than delegating to it), and its default identity-based `equals` is
 * exactly what [StagedAttachmentsReducer.remove] needs.
 */
class StagedAttachmentsReducerTest {

    private fun attachment(uri: Uri = mock(Uri::class.java), sizeBytes: Long = 100L) =
        StagedAttachment(uri = uri, displayName = "file.txt", mimeType = "text/plain", sizeBytes = sizeBytes)

    @Test
    fun `add appends every attachment under the size guard`() {
        val a = attachment()
        val b = attachment()

        val (next, rejected) = StagedAttachmentsReducer.add(emptyList(), listOf(a, b))

        assertEquals(listOf(a, b), next)
        assertTrue(rejected.isEmpty())
    }

    @Test
    fun `add rejects an attachment over the 25MB client guard without dropping the accepted ones`() {
        val small = attachment(sizeBytes = 1_000)
        val huge = attachment(sizeBytes = StagedAttachment.MAX_BYTES + 1)

        val (next, rejected) = StagedAttachmentsReducer.add(emptyList(), listOf(small, huge))

        assertEquals(listOf(small), next)
        assertEquals(listOf(huge), rejected)
    }

    @Test
    fun `add is additive onto an existing staged list`() {
        val existing = attachment()
        val incoming = attachment()

        val (next, rejected) = StagedAttachmentsReducer.add(listOf(existing), listOf(incoming))

        assertEquals(listOf(existing, incoming), next)
        assertTrue(rejected.isEmpty())
    }

    @Test
    fun `remove drops only the matching uri`() {
        val a = attachment()
        val b = attachment()

        val next = StagedAttachmentsReducer.remove(listOf(a, b), b.uri)

        assertEquals(listOf(a), next)
    }

    @Test
    fun `remove is a no-op when the uri isn't staged`() {
        val a = attachment()

        val next = StagedAttachmentsReducer.remove(listOf(a), mock(Uri::class.java))

        assertEquals(listOf(a), next)
    }

    @Test
    fun `clearOnSend empties the list for a fresh-turn send`() {
        val staged = listOf(attachment())

        val next = StagedAttachmentsReducer.clearOnSend(staged, isSteering = false)

        assertTrue(next.isEmpty())
    }

    @Test
    fun `clearOnSend preserves the list for a steer - message has no attachments field`() {
        val staged = listOf(attachment())

        val next = StagedAttachmentsReducer.clearOnSend(staged, isSteering = true)

        assertSame(staged, next)
    }
}
