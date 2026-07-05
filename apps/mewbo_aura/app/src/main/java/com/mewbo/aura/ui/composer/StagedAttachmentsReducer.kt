package com.mewbo.aura.ui.composer

import android.net.Uri
import com.mewbo.aura.data.model.StagedAttachment

/**
 * Pure add/remove/clear logic for the composer's staged-attachments chip row - extracted the same
 * way [com.mewbo.aura.ui.chat.SendDecision]/`SessionBinding` were, so it gets a plain-JVM regression
 * test without needing [com.mewbo.aura.ui.chat.ChatViewModel]'s Hilt-injected dependencies.
 */
internal object StagedAttachmentsReducer {
    /** Splits [incoming] into accepted vs. over the client-side size guard, appending only the
     * accepted half to [current]. Returns `(nextList, rejected)` so the caller can notice the user
     * about anything skipped. */
    fun add(current: List<StagedAttachment>, incoming: List<StagedAttachment>): Pair<List<StagedAttachment>, List<StagedAttachment>> {
        val (accepted, rejected) = incoming.partition { it.sizeBytes <= StagedAttachment.MAX_BYTES }
        return (current + accepted) to rejected
    }

    fun remove(current: List<StagedAttachment>, uri: Uri): List<StagedAttachment> = current.filterNot { it.uri == uri }

    /** A fresh-turn send clears the staged set (it was consumed by the upload); a steer leaves it
     * staged-but-disabled (task brief: `/message` has no attachments field, so nothing rode along -
     * dropping the chips here would silently discard the user's picks). */
    fun clearOnSend(current: List<StagedAttachment>, isSteering: Boolean): List<StagedAttachment> =
        if (isSteering) current else emptyList()
}
