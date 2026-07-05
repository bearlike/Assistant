package com.mewbo.aura.data.repo

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import com.mewbo.aura.data.api.AttachmentRecordDto
import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.model.StagedAttachment
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * Resolves a picked [Uri] into a [StagedAttachment] (display name/size/mime via [android.content.ContentResolver])
 * and, later, uploads the staged set through `POST api/sessions/{id}/attachments` - multipart,
 * repeated `files` field, exactly how the web console does it (task brief). Byte-array reads (not
 * streamed) are acceptable for v1, gated by [StagedAttachment.MAX_BYTES]'s client-side guard.
 */
class AttachmentRepository @Inject constructor(
    private val api: AuraApi,
    @ApplicationContext private val context: Context,
) {
    suspend fun resolve(uri: Uri): StagedAttachment = withContext(Dispatchers.IO) {
        var name = uri.lastPathSegment ?: "file"
        var size = 0L
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME).takeIf { it >= 0 }?.let { name = cursor.getString(it) ?: name }
                cursor.getColumnIndex(OpenableColumns.SIZE).takeIf { it >= 0 }?.let { size = cursor.getLong(it) }
            }
        }
        val mime = context.contentResolver.getType(uri) ?: "application/octet-stream"
        StagedAttachment(uri = uri, displayName = name, mimeType = mime, sizeBytes = size)
    }

    /** `model`, when present, rides the multipart `model` field so the server can 400 an image
     * against a non-vision model eagerly (`AuraApi.uploadAttachments`'s doc). */
    suspend fun upload(sessionId: String, staged: List<StagedAttachment>, model: String?): List<AttachmentRecordDto> {
        if (staged.isEmpty()) return emptyList()
        // Gitea #181 fix wave, finding 5: toPart throws instead of returning null on a revoked/
        // stale SAF grant - mapNotNull used to silently drop that attachment, so upload() could
        // return successfully having sent fewer attachments than the user picked, with no signal
        // to anyone. Letting the exception propagate routes it through ChatViewModel.send()'s
        // existing catch(Exception) -> appendClientError path (the same error-envelope convention
        // every other send failure already uses) rather than adding a new plumbing shape here.
        val parts = withContext(Dispatchers.IO) { staged.map(::toPart) }
        val modelPart = model?.takeIf { it.isNotBlank() }?.toRequestBody("text/plain".toMediaTypeOrNull())
        return api.uploadAttachments(sessionId, parts, modelPart).attachments
    }

    private fun toPart(staged: StagedAttachment): MultipartBody.Part {
        val bytes = context.contentResolver.openInputStream(staged.uri)?.use { it.readBytes() }
            ?: error("Couldn't read \"${staged.displayName}\" - it may have been removed or access was revoked")
        val body = bytes.toRequestBody(staged.mimeType.toMediaTypeOrNull())
        return MultipartBody.Part.createFormData("files", staged.displayName, body)
    }
}
