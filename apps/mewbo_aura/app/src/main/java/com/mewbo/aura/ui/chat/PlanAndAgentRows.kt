package com.mewbo.aura.ui.chat

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Person
import androidx.compose.material3.CircularProgressIndicator
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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.ChatTodoItem
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Plan card. Collapsed = a chip-family pill
 * ("Plan · {done}/{total} ✓"); expanded = the per-step checklist. Fixed `"todos"` key ([item]'s
 * own) means the reducer's replace-in-place semantics keep this ONE card updating, never appending
 * a duplicate (data/CLAUDE.md).
 */
@Composable
fun PlanCard(item: ChatItem.TodoList, modifier: Modifier = Modifier) {
    var expanded by remember { mutableStateOf(false) }
    val done = item.items.count { it.status.equals("completed", ignoreCase = true) || it.status.equals("done", ignoreCase = true) }

    // Same 24dp assistant gutter as ToolCallGroupCard/AssistantMessageRow, matching every other
    // chip-family row rather than sitting edge-to-edge.
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

/** Spec §6.11: "leading 18dp glyph". `internal` rather than `private` only because
 * `ToolCallGroupCard.kt` shares this one chip-family size — it stays beside [ChipHeight] because the
 * two are one spec clause, not two independent tokens. */
internal val ChipGlyphSize = 18.dp

/** `internal` for the same reason as [ChipGlyphSize]: `ToolCallGroupCard.kt`'s per-call row pads on
 * this same value. The name predates that second caller. */
internal val PlanStepVerticalPadding = 4.dp
