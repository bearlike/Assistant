package com.mewbo.aura.ui.common

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Metadata-only attachment indicator - one atomic tile, driven purely by (filename, mimeType): a
 * TYPE indicator ([ChatIcons.PhotoGlyph] for an `image/` mime, else a short uppercase extension label
 * such as "PDF" when one can be derived, else [ChatIcons.FileGlyph]) plus the filename, ellipsized
 * to at most two lines. NEVER wires a real thumbnail/pixels - no Coil/AsyncImage anywhere in this
 * app; this renders exactly the metadata the wire/optimistic echo carries
 * ([com.mewbo.aura.data.model.AttachmentSummary]), nothing more.
 *
 * Reused by [com.mewbo.aura.ui.chat.UserBubbleRow]'s post-send tile row above the user bubble;
 * [AttachmentGlyphs] (this file) is additionally the ONE glyph-selection source the composer's
 * pre-send `AttachmentChip` (`ui/composer/AuraComposer.kt`) also draws from, so the `image/`-mime
 * check is never duplicated across the two surfaces even though their chrome (square tile vs. pill chip)
 * deliberately differs.
 */
@Composable
fun AttachmentTile(filename: String, mimeType: String, modifier: Modifier = Modifier) {
    Surface(
        color = AuraColors.surfaceInput,
        shape = RoundedCornerShape(AuraShape.radiusCard),
        modifier = modifier.size(AuraSpacing.AttachmentTile.size),
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier
                .fillMaxSize()
                .padding(AuraSpacing.AttachmentTile.internalPadding),
        ) {
            Box(modifier = Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                val label = AttachmentGlyphs.shortTypeLabel(filename, mimeType)
                if (label != null) {
                    Text(text = label, style = AuraType.chipLabel, color = AuraColors.textSecondary)
                } else {
                    Icon(
                        imageVector = AttachmentGlyphs.forMimeType(mimeType),
                        contentDescription = null,
                        tint = AuraColors.textSecondary,
                        modifier = Modifier.size(AuraSpacing.Composer.iconSize),
                    )
                }
            }
            Text(
                text = filename,
                style = AuraType.caption,
                color = AuraColors.textSecondary,
                textAlign = TextAlign.Center,
                maxLines = AttachmentFilenameMaxLines,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

/**
 * Shared (filename, mimeType) -> type indicator resolution, the ONE place the `image/`-mime check
 * lives - both [AttachmentTile] and the composer's pre-send `AttachmentChip` drive their glyph
 * from this instead of each duplicating it (same "one atomic helper, two chrome styles" shape as
 * `ui/chat/ChatMessageRows.kt`'s existing `ActivityToolGlyphs`).
 */
object AttachmentGlyphs {
    fun forMimeType(mimeType: String): ImageVector =
        if (mimeType.startsWith("image/")) ChatIcons.PhotoGlyph else ChatIcons.FileGlyph

    /**
     * A short uppercase extension label (e.g. "PDF") for a non-image file whose name carries one -
     * `null` for images (the glyph alone is enough) and for names with no short (2-4 char)
     * extension, where [forMimeType]'s [ChatIcons.FileGlyph] fallback takes over.
     */
    fun shortTypeLabel(filename: String, mimeType: String): String? {
        if (mimeType.startsWith("image/")) return null
        val ext = filename.substringAfterLast('.', missingDelimiterValue = "").uppercase()
        return ext.takeIf { it.length in MIN_EXT_LENGTH..MAX_EXT_LENGTH }
    }

    private const val MIN_EXT_LENGTH = 2
    private const val MAX_EXT_LENGTH = 4
}

private const val AttachmentFilenameMaxLines = 2
