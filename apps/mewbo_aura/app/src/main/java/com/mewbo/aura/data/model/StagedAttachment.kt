package com.mewbo.aura.data.model

import android.net.Uri
import androidx.compose.runtime.Immutable

/** A file picked in the composer's "+" sheet but not yet uploaded (Photos/Files pickers, Gitea
 * #177 W2) - resolved once via [com.mewbo.aura.data.repo.AttachmentRepository.resolve] so the chip
 * row and the eventual upload read the same cached metadata instead of re-querying per composition. */
@Immutable
data class StagedAttachment(
    val uri: Uri,
    val displayName: String,
    val mimeType: String,
    val sizeBytes: Long,
) {
    companion object {
        /** Client-side guard (task brief) - the backend documents no upload size cap of its own. */
        const val MAX_BYTES: Long = 25L * 1024 * 1024
    }
}
