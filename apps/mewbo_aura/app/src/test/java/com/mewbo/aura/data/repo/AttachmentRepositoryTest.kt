package com.mewbo.aura.data.repo

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.model.StagedAttachment
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.mockito.Mockito.mock
import org.mockito.Mockito.`when`

/**
 * [AttachmentRepository.upload] must fail loud (throw) rather than
 * silently sending fewer attachments than the user picked when a staged [Uri]'s stream can't be
 * opened (a revoked/stale SAF grant). `Context`/`ContentResolver` mock the same way this module's
 * `Uri` idiom does (`app/src/test/.../CLAUDE.md`) - Mockito's default mock maker bypasses the real
 * android.jar stub bodies (which throw "not mocked" outside Robolectric) entirely, so an unstubbed
 * method on any of these three mocks returns a safe default rather than throwing.
 */
class AttachmentRepositoryTest {

    private fun staged(uri: Uri, displayName: String = "notes.txt") =
        StagedAttachment(uri = uri, displayName = displayName, mimeType = "text/plain", sizeBytes = 100L)

    @Test
    fun `upload throws instead of silently dropping an attachment whose stream can't be opened`() = runTest {
        val uri = mock(Uri::class.java)
        val contentResolver = mock(ContentResolver::class.java)
        `when`(contentResolver.openInputStream(uri)).thenReturn(null)
        val context = mock(Context::class.java)
        `when`(context.contentResolver).thenReturn(contentResolver)
        val api = mock(AuraApi::class.java)
        val repo = AttachmentRepository(api, context)

        val thrown = try {
            repo.upload("session-1", listOf(staged(uri)), model = null)
            null
        } catch (e: IllegalStateException) {
            e
        }

        assertNotNull("expected upload() to throw rather than silently drop the attachment", thrown)
        assertTrue(thrown!!.message.orEmpty().contains("notes.txt"))
    }

    @Test
    fun `upload is a no-op for an empty staged list - never touches the resolver or the api`() = runTest {
        val context = mock(Context::class.java)
        val api = mock(AuraApi::class.java)
        val repo = AttachmentRepository(api, context)

        val records = repo.upload("session-1", emptyList(), model = null)

        assertTrue(records.isEmpty())
    }
}
