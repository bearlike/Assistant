package com.mewbo.aura.ui.chat

import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import com.mewbo.aura.data.model.AttachmentSummary
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.common.AttachmentTile
import com.mewbo.aura.ui.common.auraFocusRing
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * User's own message (spec §6.3): right-aligned stadium bubble, max-width a FRACTION of the
 * available width (not a fixed cap - [BoxWithConstraints] reads the real constraint so a short
 * message doesn't stretch). [overWash] swaps the fill while an aurora wash is active behind the
 * transcript.
 *
 * **Long-press is EITHER the actions sheet OR system text-selection, never both** — the two gestures
 * are the same gesture, and [SelectionContainer] is a CHILD of the bubble, so it wins the pointer
 * event and a parent `combinedClickable` would simply never fire. [onLongPress] non-null (the
 * transcript passes it whenever [MessageAction.anyAvailableFor] says the bubble has any action at
 * all) therefore swaps the selection wrapper OUT for the gesture; `null` (only a host that wires
 * nothing — the assist overlay) keeps the original select-to-copy behavior exactly as it was.
 *
 * **Displacing select-to-copy is precisely why [MessageAction.Copy] exists**, and why it is the one
 * action nothing can withhold: the sheet must give back the capability the gesture takes away, or
 * long-press is a net accessibility LOSS. Do not remove that row without restoring the
 * [SelectionContainer] here.
 *
 * The gesture carries no tap action of its own ([indication] `null`, so no ripple on a stray tap —
 * a message bubble is not a button); `combinedClickable`'s own `hapticFeedbackEnabled` (default
 * `true` on this Compose Foundation version) fires the one long-press haptic, exactly as
 * `AuraDrawerContent`'s Recents row does.
 *
 * [ChatItem.UserBubble.attachments], when non-empty, renders as its own right-aligned
 * [AttachmentTileRow] ABOVE the bubble, sharing the bubble's own [AuraSpacing.UserBubble.rightMargin]
 * so both edges line up - metadata-only tiles (filename + type), never a real thumbnail.
 */
@Composable
fun UserBubbleRow(
    item: ChatItem.UserBubble,
    modifier: Modifier = Modifier,
    overWash: Boolean = false,
    // Takes the item-level callback (not a pre-bound `() -> Unit`) so ChatTranscript's per-item
    // dispatch passes this reference straight through unchanged instead of allocating a fresh
    // `{ onLongPress(item) }` closure on every invocation - the exact per-row stability trap
    // AssistantMessageRow's onReadAloudToggle documents (ui/CLAUDE.md "Compose stability").
    onLongPress: ((ChatItem.UserBubble) -> Unit)? = null,
) {
    BoxWithConstraints(modifier = modifier.fillMaxWidth()) {
        val maxBubbleWidth = maxWidth * AuraSpacing.UserBubble.maxWidthFraction
        val interactionSource = remember { MutableInteractionSource() }
        Column(modifier = Modifier.fillMaxWidth(), horizontalAlignment = Alignment.End) {
            if (item.attachments.isNotEmpty()) {
                AttachmentTileRow(
                    attachments = item.attachments,
                    modifier = Modifier
                        .widthIn(max = maxBubbleWidth)
                        .padding(end = AuraSpacing.UserBubble.rightMargin, bottom = AuraSpacing.AttachmentTile.gapToBubble)
                        .alpha(if (item.pending) QueuedSendAlpha else 1f),
                )
            }
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                Surface(
                    color = if (overWash) AuraColors.surfaceBubbleOnWash else AuraColors.surfaceInput,
                    shape = RoundedCornerShape(AuraShape.radiusBubble),
                    modifier = Modifier
                        .widthIn(max = maxBubbleWidth)
                        .padding(end = AuraSpacing.UserBubble.rightMargin)
                        .alpha(if (item.pending) QueuedSendAlpha else 1f)
                        .then(
                            if (onLongPress == null) {
                                Modifier
                            } else {
                                Modifier
                                    .auraFocusRing(shape = RoundedCornerShape(AuraShape.radiusBubble))
                                    .combinedClickable(
                                        interactionSource = interactionSource,
                                        indication = null,
                                        onLongClickLabel = "Message actions",
                                        onLongClick = { onLongPress(item) },
                                        onClick = {},
                                    )
                            },
                        ),
                ) {
                    if (onLongPress == null) {
                        SelectionContainer { UserBubbleText(item.text) }
                    } else {
                        UserBubbleText(item.text)
                    }
                }
            }
        }
    }
}

/** The bubble's text, factored out ONLY so the selection-vs-long-press swap above wraps one shared
 * declaration instead of duplicating it (Compose has no way to conditionally apply a wrapper
 * composable without either duplicating the content or hoisting it like this). */
@Composable
private fun UserBubbleText(text: String) {
    Text(
        text = text,
        style = AuraType.bodyMessage,
        color = AuraColors.textPrimary,
        modifier = Modifier.padding(
            horizontal = AuraSpacing.UserBubble.paddingHorizontal,
            vertical = AuraSpacing.UserBubble.paddingVertical,
        ),
    )
}

/** Right-aligned, horizontally-scrolling row of [AttachmentTile]s (wrap/scroll for multiples) -
 * the transcript's post-send counterpart to the composer's pre-send `AttachmentChipRow`. Not
 * `fillMaxWidth` itself: sizing to its own content (capped by [modifier]'s `widthIn`) is what lets
 * the parent [Column]'s `Alignment.End` flush it against the bubble's own right edge. */
@Composable
private fun AttachmentTileRow(attachments: List<AttachmentSummary>, modifier: Modifier = Modifier) {
    Row(
        horizontalArrangement = Arrangement.spacedBy(AuraSpacing.AttachmentTile.interTileGap),
        modifier = modifier.horizontalScroll(rememberScrollState()),
    ) {
        attachments.forEach { attachment ->
            AttachmentTile(filename = attachment.filename, mimeType = attachment.mimeType)
        }
    }
}

/** Spec §6.12: queued (202-enqueued) sends render at 70% opacity until the server's real echo
 * settles them (reducer flip, see `TranscriptReducer.foldUserText`). */
private const val QueuedSendAlpha = 0.7f
