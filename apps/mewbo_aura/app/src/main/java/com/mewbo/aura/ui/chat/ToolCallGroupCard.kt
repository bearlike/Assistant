package com.mewbo.aura.ui.chat

import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.tween
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
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
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.ToolCall
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
 * Minimal, cardless fold group for one turn's `tool_result`s (restyled
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
    // pins the user's own choice from then on. (Auto-expanding on every live run was
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
