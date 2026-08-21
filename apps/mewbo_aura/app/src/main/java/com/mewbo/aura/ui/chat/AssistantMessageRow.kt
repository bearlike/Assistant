package com.mewbo.aura.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material3.Icon
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.common.ChatOverflowMenu
import com.mewbo.aura.ui.common.MarkdownBuffer
import com.mewbo.aura.ui.common.MarkdownMessage
import com.mewbo.aura.ui.common.OverflowMenuItem
import com.mewbo.aura.ui.common.auraFocusRing
import com.mewbo.aura.ui.common.rememberStreamedText
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Assistant reply (spec §6.3/§6.4): bare full-width text, no bubble/surface. Both streaming AND
 * finalized text render through [MarkdownMessage] - streaming shows real markdown as it arrives.
 * The buffer always passes through [MarkdownBuffer.sanitize] first, which exists precisely for
 * half-open mid-stream markdown (an unclosed fence/bracket that would otherwise explode layout) as
 * much as for the case where streaming closed before the authoritative
 * `assistant` finalize event did (ui/CLAUDE.md). [ChatItem.AssistantMessage.isStreaming] still
 * flows through unchanged for every other consumer (e.g. [ChatTranscript]'s `isRunLive`/
 * `hasSettledReply`) - it just no longer selects between two different renderers here.
 * [showActionRow] is true for every SETTLED (non-streaming) assistant message: the footer renders
 * under every completed response, not just the last, so
 * [ChatTranscript] derives it per row from the item's own `isStreaming`, not a chosen-row scan. The
 * "Mewbo is an AI tool and can make mistakes" disclaimer is NOT this row's concern - it's anchored to a turn's LAST
 * item (which may not be an AssistantMessage at all, e.g. a turn that ends on a tool call), so
 * [ChatTranscript] renders it as its own sibling, not a property here.
 */
@Composable
fun AssistantMessageRow(
    item: ChatItem.AssistantMessage,
    showActionRow: Boolean,
    isSpeaking: Boolean,
    onNotice: (String) -> Unit,
    // Takes the item-level callback (not a pre-bound () -> Unit) so ChatTranscript's per-item
    // dispatch can pass this reference straight through unchanged instead of allocating a fresh
    // `{ onReadAloudToggle(item) }` closure every time that call site runs - a fresh lambda there
    // broke this row's parameter stability and forced it to recompose on every unrelated sibling
    // update, not just its own deltas (a recomposition-count measurement caught this: 16 hits observed
    // for 12 deltas + 1 mount, the extra 3 lining up with 3 unrelated chip arrivals).
    onReadAloudToggle: (ChatItem.AssistantMessage) -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxWidth().padding(horizontal = AuraSpacing.AssistantText.gutter)) {
        // rememberStreamedText throttles the per-token reparse while streaming (~20 Hz); the sanitize
        // guard then closes any half-open fence/bracket in that throttled snapshot. Settled rows
        // (isStreaming=false) pass straight through, so finalized markdown is unchanged.
        MarkdownMessage(text = MarkdownBuffer.sanitize(rememberStreamedText(item.text, item.isStreaming)))

        val density = LocalDensity.current
        AnimatedVisibility(
            visible = showActionRow,
            enter = fadeIn(tween(AuraMotion.actionRowFadeMs)) +
                slideInVertically(tween(AuraMotion.actionRowFadeMs)) { with(density) { AuraMotion.actionRowRise.roundToPx() } },
        ) {
            ActionRow(
                messageText = item.text,
                isSpeaking = isSpeaking,
                onNotice = onNotice,
                onReadAloudToggle = { onReadAloudToggle(item) },
                modifier = Modifier.padding(top = AuraSpacing.ActionRow.topMargin),
            )
        }
    }
}

private enum class ThumbVote { Up, Down }

/**
 * Spec §6.5 (Rev E §E-4 corrected: no visible ↻ regenerate). Thumbs are visible first-class
 * buttons with a LOCAL-ONLY toggle (v1: no backend endpoint, no fake persistence - brief). Copy
 * writes the clipboard directly (leaf composable, [LocalClipboardManager] is available right here)
 * and fires [onNotice]; read-aloud active state is entirely caller-driven via [isSpeaking].
 */
@Composable
private fun ActionRow(
    messageText: String,
    isSpeaking: Boolean,
    onNotice: (String) -> Unit,
    onReadAloudToggle: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val clipboard = LocalClipboardManager.current
    var vote by remember { mutableStateOf<ThumbVote?>(null) }
    var overflowExpanded by remember { mutableStateOf(false) }
    var selectTextOpen by remember { mutableStateOf(false) }

    // Touching 48dp cellSize touch-cells, zero extra inter-icon gap: minimumInteractiveComponentSize()
    // below already pads each icon out to 48dp on its own, so an explicit spacedBy gap on top of that
    // would double-count the padding and inflate the pitch past its intended 48dp. No
    // horizontalArrangement here (default Arrangement.Start already has zero gap - no dp literal
    // needed to say "none").
    Row(verticalAlignment = Alignment.CenterVertically, modifier = modifier.fillMaxWidth()) {
        Row {
            ActionGlyphButton(
                icon = Icons.Filled.ThumbUp,
                description = "Good response",
                tinted = vote == ThumbVote.Up,
                onClick = { vote = if (vote == ThumbVote.Up) null else ThumbVote.Up },
            )
            ActionGlyphButton(
                icon = Icons.Filled.ThumbUp,
                description = "Bad response",
                tinted = vote == ThumbVote.Down,
                onClick = { vote = if (vote == ThumbVote.Down) null else ThumbVote.Down },
                modifier = Modifier.graphicsLayer(rotationZ = 180f), // thumb_down = thumb_up rotated 180°
            )
            ActionGlyphButton(
                icon = ChatIcons.ContentCopy,
                description = "Copy",
                onClick = {
                    clipboard.setText(AnnotatedString(messageText))
                    onNotice("Copied")
                },
            )
            Box {
                ActionGlyphButton(icon = Icons.Filled.MoreVert, description = "More", onClick = { overflowExpanded = true })
                ChatOverflowMenu(
                    expanded = overflowExpanded,
                    onDismiss = { overflowExpanded = false },
                    items = listOf(
                        OverflowMenuItem(icon = Icons.Filled.List, label = "Select text") {
                            overflowExpanded = false
                            selectTextOpen = true
                        },
                    ),
                )
            }
        }
        Spacer(modifier = Modifier.weight(1f))
        ReadAloudButton(isSpeaking = isSpeaking, onClick = onReadAloudToggle)
    }

    if (selectTextOpen) {
        SelectTextDialog(text = messageText, onDismiss = { selectTextOpen = false })
    }
}

@Composable
private fun ActionGlyphButton(
    icon: ImageVector,
    description: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    tinted: Boolean = false,
) {
    // Explicit cellSize (not minimumInteractiveComponentSize(), which independently pads out to
    // 48dp too) - Rev F's fix keeps the cell size explicit and singular so the Row's icons touch
    // flush edge-to-edge rather than each carrying its own implicit reserve.
    Box(
        modifier = modifier
            .size(AuraSpacing.ActionRow.cellSize)
            .auraFocusRing(shape = CircleShape)
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            imageVector = icon,
            contentDescription = description,
            tint = if (tinted) AuraColors.accentPrimary else AuraColors.iconPrimary.copy(alpha = ActionGlyphOpacity),
            modifier = Modifier.size(AuraSpacing.ActionRow.iconSize),
        )
    }
}

/** Spec §6.5: "🔊 pinned trailing... active state = glyph tints accentPrimary + surfaceIconScrim
 * circle while SynthEvent.Started..Done". */
@Composable
private fun ReadAloudButton(isSpeaking: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .size(AuraSpacing.ActionRow.cellSize) // same touch-cell family as ActionGlyphButton (Rev F)
            .then(if (isSpeaking) Modifier.background(AuraColors.surfaceIconScrim, CircleShape) else Modifier)
            .auraFocusRing(shape = CircleShape)
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            imageVector = ChatIcons.VolumeUp,
            contentDescription = if (isSpeaking) "Stop reading aloud" else "Read aloud",
            tint = if (isSpeaking) AuraColors.accentPrimary else AuraColors.iconPrimary.copy(alpha = ActionGlyphOpacity),
            modifier = Modifier.size(AuraSpacing.ActionRow.iconSize),
        )
    }
}

/** Spec §6.5: "24dp icons @80% opacity". */
private const val ActionGlyphOpacity = 0.8f

/** Per-message overflow's one working item (spec §6.5): the full message text in a selectable
 * view, since the bare assistant text itself carries no [SelectionContainer] (the copy button
 * already covers that case; this is for selecting a SUBSTRING). */
@Composable
private fun SelectTextDialog(text: String, onDismiss: () -> Unit) {
    Dialog(onDismissRequest = onDismiss) {
        Surface(color = AuraColors.surfaceCanvas, shape = RoundedCornerShape(AuraShape.radiusThumb)) {
            SelectionContainer {
                Text(
                    text = text,
                    style = AuraType.bodyMessage,
                    color = AuraColors.textPrimary,
                    modifier = Modifier
                        .heightIn(max = SelectTextDialogMaxHeight)
                        .verticalScroll(rememberScrollState())
                        .padding(AuraSpacing.Composer.internalPadding),
                )
            }
        }
    }
}

private val SelectTextDialogMaxHeight = 480.dp
