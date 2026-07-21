package com.mewbo.aura.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.mewbo.aura.data.model.AttachmentSummary
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.ChatTodoItem
import com.mewbo.aura.data.model.ToolCall
import com.mewbo.aura.ui.common.AttachmentTile
import com.mewbo.aura.ui.common.ChatOverflowMenu
import com.mewbo.aura.ui.common.MarkdownBuffer
import com.mewbo.aura.ui.common.MarkdownMessage
import com.mewbo.aura.ui.common.OverflowMenuItem
import com.mewbo.aura.ui.common.rememberStreamedText
import com.mewbo.aura.ui.common.TypingIndicator
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

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
                                Modifier.combinedClickable(
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

/**
 * Assistant reply (spec §6.3/§6.4): bare full-width text, no bubble/surface. Both streaming AND
 * finalized text render through [MarkdownMessage] now (user directive 2026-07-04: streaming shows
 * real markdown as it arrives, retiring the old [com.mewbo.aura.ui.common.WordFadeText] word-fade
 * comet-tail) - the buffer always passes through [MarkdownBuffer.sanitize] first, which exists
 * precisely for half-open mid-stream markdown (an unclosed fence/bracket that would otherwise
 * explode layout) as much as for the case where streaming closed before the authoritative
 * `assistant` finalize event did (ui/CLAUDE.md). [ChatItem.AssistantMessage.isStreaming] still
 * flows through unchanged for every other consumer (e.g. [ChatTranscript]'s `isRunLive`/
 * `hasSettledReply`) - it just no longer selects between two different renderers here.
 * [showActionRow] is true for every SETTLED (non-streaming) assistant message: the footer renders
 * under every completed response now, not just the last (user directive 2026-07-04 round 3), so
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
    // update, not just its own deltas (fix-round item 2 measurement caught this: 16 hits observed
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

    // Rev F measured (c3c1011): touching 48dp cellSize touch-cells, zero extra inter-icon gap -
    // the old iconSize(24dp)+gap(28dp) math predicted a 52dp pitch but rendered at 76dp on device,
    // because minimumInteractiveComponentSize() below ALSO pads each icon out to 48dp on its own;
    // stacking that with an explicit spacedBy gap double-counted the padding. No horizontalArrangement
    // here now (default Arrangement.Start already has zero gap - no dp literal needed to say "none").
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

/**
 * Minimal, cardless fold group for one turn's `tool_result`s (restyled 2026-07-03
 * per the "no fat tool cards" brief) - a plain row, never a filled Surface/card. Collapsed header
 * always shows the tool count ("Using N tools…" while [isRunActive], "Used N tools" once the run
 * settles) so the count stays visible without expanding; tapping it reveals one [ToolCallRow] per
 * call, each independently expandable to its own input/result detail. Inset to the 24dp assistant
 * gutter via the outer [Column]'s padding; hierarchy reads through that indentation plus a
 * hairline divider on expand, never a background fill.
 */
@Composable
fun ToolCallGroupCard(item: ChatItem.ToolCallGroup, isRunActive: Boolean, modifier: Modifier = Modifier) {
    // null = "no explicit choice yet" -> always starts COLLAPSED regardless of isRunActive; a tap
    // pins the user's own choice from then on. (2026-07-03: auto-expanding on every live run was
    // the "why is this open every time" complaint - live progress now reads through the header's
    // own count label + pulse instead of forcing the whole group open.)
    var userExpanded by remember(item.key) { mutableStateOf<Boolean?>(null) }
    val expanded = userExpanded ?: false

    Column(modifier = modifier.fillMaxWidth().padding(horizontal = AuraSpacing.AssistantText.gutter)) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
            modifier = Modifier
                .fillMaxWidth()
                .height(AuraSpacing.ActivityGroup.rowHeight)
                .clickable(onClick = { userExpanded = !expanded }),
        ) {
            Icon(
                imageVector = Icons.Filled.Build,
                contentDescription = null,
                tint = AuraColors.textSecondary,
                modifier = Modifier.size(ChipGlyphSize),
            )
            if (isRunActive) {
                // Reuses the shared three-dot pulse (ui/common/TypingIndicator) rather than a
                // bespoke animation - the "quiet pulse/typing-dot treatment" the brief asks for
                // already exists as one atomic component. CompositionLocalProvider pins the dot
                // color to textSecondary: TypingIndicator paints its dots from ambient
                // LocalContentColor, left undefined by default on this backgroundless row.
                CompositionLocalProvider(LocalContentColor provides AuraColors.textSecondary) {
                    TypingIndicator(label = toolGroupHeaderLabel(item.calls.size, isActive = true), modifier = Modifier.weight(1f))
                }
            } else {
                Text(
                    text = toolGroupHeaderLabel(item.calls.size, isActive = false),
                    style = AuraType.chipLabel,
                    color = AuraColors.textSecondary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
            }
            Icon(
                imageVector = Icons.Filled.KeyboardArrowDown,
                contentDescription = if (expanded) "Collapse tool calls" else "Expand tool calls",
                tint = AuraColors.textSecondary,
                modifier = Modifier
                    .size(ChipGlyphSize)
                    .graphicsLayer(rotationZ = if (expanded) HALF_TURN_DEGREES else 0f),
            )
        }

        if (expanded) {
            HorizontalDivider(color = AuraColors.outlineHairline)
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(start = ToolCallDetailIndent)
                    .padding(bottom = AuraSpacing.Composer.gapTight),
            ) {
                // A hairline BETWEEN consecutive calls (never after the last) so each one reads as
                // its own distinct row even when several sit expanded at once - the group's own
                // header divider above already separates the whole block from the header.
                item.calls.forEachIndexed { index, call ->
                    if (index > 0) HorizontalDivider(color = AuraColors.outlineHairline)
                    ToolCallRow(call = call)
                }
            }
        }
    }
}

/** Tool count label, always visible - the count-even-when-closed ask: "Using N tools…" mid-run,
 * "Used N tools" once settled (unchanged text for the settled case, only the active case gained
 * its count). */
private fun toolGroupHeaderLabel(callCount: Int, isActive: Boolean): String {
    val noun = if (callCount == 1) "tool" else "tools"
    return if (isActive) "Using $callCount $noun…" else "Used $callCount $noun"
}

private const val HALF_TURN_DEGREES = 180f

/** Extra start-inset for the expanded per-call rows, beyond the group's own 24dp gutter padding -
 * the "minor indentation to show hierarchy" the brief asks for, replacing the deleted filled-card
 * nesting. Reuses the composer's existing internal-padding token rather than inventing a new one. */
private val ToolCallDetailIndent = AuraSpacing.Composer.internalPadding

/**
 * One [ToolCall] inside an expanded [ToolCallGroupCard]. Collapsed (the default) shows only the
 * tool name plus an optional one-line summary and its status glyph; a tap reveals the pretty-
 * printed input JSON and the result text in place. [reducedMotion] drops the size-change animation
 * entirely rather than shortening it (M8 spirit, data/CLAUDE.md).
 */
@Composable
private fun ToolCallRow(call: ToolCall, modifier: Modifier = Modifier) {
    var expanded by remember(call.key) { mutableStateOf(false) }
    val reducedMotion = LocalAssistantExtras.current.reducedMotion

    Column(
        modifier = modifier
            .fillMaxWidth()
            .then(if (reducedMotion) Modifier else Modifier.animateContentSize(tween(AuraMotion.actionRowFadeMs)))
            .clickable(onClick = { expanded = !expanded })
            .padding(vertical = PlanStepVerticalPadding),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight)) {
            Icon(
                imageVector = ActivityToolGlyphs.forToolId(call.toolId),
                contentDescription = null,
                tint = AuraColors.textSecondary,
                modifier = Modifier.size(ChipGlyphSize),
            )
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = call.toolId,
                    style = AuraType.chipLabel.copy(fontFamily = FontFamily.Monospace),
                    color = AuraColors.textSecondary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                // Only shown once expanded - collapsed, it read as a confusing second line of
                // near-identical text right under the title (same color, near-same size). The
                // title alone is enough to identify a collapsed row; the summary belongs with the
                // rest of the detail a tap reveals, not duplicated above it.
                if (expanded && call.summary != null) {
                    Text(
                        text = call.summary,
                        style = AuraType.caption,
                        color = AuraColors.textTertiary,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            ChipStatusGlyph(success = call.success)
        }

        if (expanded) {
            Column(modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight)) {
                Text(text = "Input", style = AuraType.caption, color = AuraColors.textTertiary)
                val prettyInput = remember(call.inputJson) {
                    call.inputJson?.let { PrettyJson.encodeToString(it) } ?: "{}"
                }
                ToolCallCodeBlock(text = prettyInput, modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight))

                Text(
                    text = "Result",
                    style = AuraType.caption,
                    color = AuraColors.textTertiary,
                    modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
                )
                ToolCallCodeBlock(
                    text = call.detail ?: call.error ?: "(no result)",
                    modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
                )
            }
        }
    }
}

/** Formatted code block shared by BOTH a tool call's pretty-printed input JSON and its result/
 * error text (spec: "formatted JSON code block" - a result is very often markdown/plain prose, but
 * it's still tool output rather than assistant prose, so it gets the exact same monospace/
 * contrast-surface treatment as the input instead of reading as an unstyled orphan line). A local
 * one-off [Json] instance - `di/DataModule.kt`'s injected [Json] is the wire decoder
 * (`ignoreUnknownKeys`, no pretty-printing) and stays that way for every other call site. */
@Composable
private fun ToolCallCodeBlock(text: String, modifier: Modifier = Modifier) {
    Surface(color = AuraColors.surfaceSelected, shape = RoundedCornerShape(AuraShape.radiusThumb), modifier = modifier.fillMaxWidth()) {
        Text(
            text = text,
            style = AuraType.caption.copy(fontFamily = FontFamily.Monospace),
            color = AuraColors.textSecondary,
            modifier = Modifier
                .heightIn(max = AuraSpacing.ActivityGroup.detailMaxHeight)
                .verticalScroll(rememberScrollState())
                .horizontalScroll(rememberScrollState())
                .padding(AuraSpacing.Composer.gapTight),
        )
    }
}

private val PrettyJson = Json { prettyPrint = true }

@Composable
private fun ChipStatusGlyph(success: Boolean, modifier: Modifier = Modifier) {
    Icon(
        imageVector = if (success) Icons.Filled.Check else Icons.Filled.Close,
        contentDescription = if (success) "Succeeded" else "Failed",
        tint = if (success) AuraColors.textSecondary else AuraColors.accentError,
        modifier = modifier.size(ChipGlyphSize),
    )
}

/** Per-`toolId` leading glyph (spec §6.11), seeded with the backend's real tool families -
 * unrecognized ids fall through to a generic default, never crash on an unknown one (data/CLAUDE.md
 * forward-compat spirit, applied to display rather than parsing). */
private object ActivityToolGlyphs {
    fun forToolId(toolId: String): ImageVector = when {
        toolId.startsWith("mcp") -> Icons.Filled.Settings
        toolId.contains("search", ignoreCase = true) || toolId.contains("web", ignoreCase = true) -> Icons.Filled.Search
        toolId.contains("file", ignoreCase = true) || toolId.contains("edit", ignoreCase = true) || toolId.contains("read", ignoreCase = true) -> Icons.Filled.Edit
        toolId == "spawn_agent" -> Icons.Filled.Person
        toolId == "update_todos" -> Icons.Filled.CheckCircle
        toolId.contains("shell", ignoreCase = true) || toolId.contains("exec", ignoreCase = true) -> Icons.Filled.PlayArrow
        else -> Icons.Filled.Build
    }
}

/**
 * Plan card (Rev D §D-5, replaces the old `TodoListRow` visuals). Collapsed = a chip-family pill
 * ("Plan · {done}/{total} ✓"); expanded = the per-step checklist. Fixed `"todos"` key ([item]'s
 * own) means the reducer's replace-in-place semantics keep this ONE card updating, never appending
 * a duplicate (data/CLAUDE.md).
 */
@Composable
fun PlanCard(item: ChatItem.TodoList, modifier: Modifier = Modifier) {
    var expanded by remember { mutableStateOf(false) }
    val done = item.items.count { it.status.equals("completed", ignoreCase = true) || it.status.equals("done", ignoreCase = true) }

    // Same 24dp assistant gutter as ToolCallGroupCard/AssistantMessageRow
    // (alignment fix) - previously edge-to-edge, unlike every other chip-family row.
    Column(modifier = modifier.fillMaxWidth().padding(horizontal = AuraSpacing.AssistantText.gutter)) {
        Surface(
            color = AuraColors.surfaceInput,
            shape = AuraShape.radiusPill,
            modifier = Modifier
                .height(ChipHeight)
                .clickable(onClick = { expanded = !expanded }),
        ) {
            Box(
                contentAlignment = Alignment.CenterStart,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = AuraSpacing.Composer.internalPadding),
            ) {
                Text(text = "Plan · $done/${item.items.size} ✓", style = AuraType.chipLabel, color = AuraColors.textSecondary)
            }
        }

        if (expanded) {
            Surface(
                color = AuraColors.surfaceInput,
                shape = RoundedCornerShape(AuraShape.radiusThumb),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = AuraSpacing.Composer.gapTight),
            ) {
                Column(modifier = Modifier.padding(AuraSpacing.Composer.gapTight)) {
                    item.items.forEach { todo -> PlanStepRow(todo) }
                }
            }
        }
    }
}

@Composable
private fun PlanStepRow(todo: ChatTodoItem, modifier: Modifier = Modifier) {
    val (glyph, tint) = when {
        todo.status.equals("completed", ignoreCase = true) || todo.status.equals("done", ignoreCase = true) ->
            "✓" to AuraColors.textSecondary
        todo.status.equals("in_progress", ignoreCase = true) -> "◐" to AuraColors.accentPrimary
        else -> "○" to AuraColors.textSecondary
    }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = PlanStepVerticalPadding),
    ) {
        Text(text = glyph, color = tint, style = AuraType.listItem)
        Text(text = todo.label, style = AuraType.listItem, color = AuraColors.textPrimary)
    }
}

/**
 * A sub-agent's lifecycle (Rev D §D-5). One item per `agentId`, upserted in place by the reducer -
 * this row just renders whatever [ChatItem.AgentChip] snapshot it's handed. Distinguished from
 * [ToolCallGroupCard] by a fixed agent-silhouette glyph rather than a per-`toolId` lookup, and an
 * in-progress spinner where [ChatItem.AgentChip.terminal] is still false.
 */
@Composable
fun AgentChipRow(item: ChatItem.AgentChip, modifier: Modifier = Modifier) {
    Surface(
        color = AuraColors.surfaceInput,
        shape = AuraShape.radiusPill,
        // Same 24dp assistant gutter as ToolCallGroupCard/PlanCard (alignment fix).
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.AssistantText.gutter)
            .height(ChipHeight),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.Composer.internalPadding),
        ) {
            Icon(
                imageVector = Icons.Filled.Person,
                contentDescription = null,
                tint = AuraColors.textSecondary,
                modifier = Modifier.size(ChipGlyphSize),
            )
            Text(
                text = "${item.agentType ?: item.agentId.take(AGENT_SHORT_ID_LENGTH)} · ${item.status ?: "working"}",
                style = AuraType.chipLabel,
                color = AuraColors.textSecondary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            if (item.terminal) {
                Icon(
                    imageVector = if (item.success == true) Icons.Filled.Check else Icons.Filled.Close,
                    contentDescription = if (item.success == true) "Succeeded" else "Failed",
                    tint = if (item.success == true) AuraColors.textSecondary else AuraColors.accentError,
                    modifier = Modifier.size(ChipGlyphSize),
                )
            } else {
                CircularProgressIndicator(
                    strokeWidth = AgentChipSpinnerStroke,
                    color = AuraColors.textSecondary,
                    modifier = Modifier.size(ChipGlyphSize),
                )
            }
        }
    }
}

private const val AGENT_SHORT_ID_LENGTH = 8
private val AgentChipSpinnerStroke = 2.dp

/** Spec §6.11/D-5: "36dp height" pill for both activity chips and the plan card / agent chips -
 * same chip family, one shared size. No matching [AuraSpacing] token yet; flagged in the report. */
private val ChipHeight = 36.dp

/** Spec §6.11: "leading 18dp glyph". */
private val ChipGlyphSize = 18.dp

private val PlanStepVerticalPadding = 4.dp
