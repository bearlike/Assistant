package com.mewbo.aura.ui.chat.toolcards

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
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
import androidx.compose.ui.text.style.TextOverflow
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * The one base component every promoted tool renders through ([ToolCardRegistry] dispatches to a
 * bespoke renderer that composes THIS, or to the generic fallback that also composes this) - a
 * filled card that LIFTS the call out of the collapsed `ToolCallGroupCard` fold and gives it its
 * own surface.
 *
 * It does NOT suppress the turn's prose reply. The wire guarantees exactly one `assistant` event
 * per turn and the reducer never drops it, so a promoted turn renders `[card] → [prose]` - the card
 * ABOVE the narration (activity-precedes-narration, `data/CLAUDE.md`), never instead of it. The
 * reference app does suppress its prose; we deliberately do not, because a turn's narration is not
 * ours to delete - it may carry more than the tool's own result. Do not write copy here that assumes
 * the card is the whole answer. Anatomy is the reference's "action card" (GMS device capture,
 * 2026-07-12): a header that answers *which tool is this* (glyph + label), then a swappable content
 * slot carrying the tool's own payload. Every value is an Aura token; nothing here is measured off
 * the reference's pixels except the ANATOMY.
 *
 * Deliberately NOT interactive: no chevron, no expand, no click. The card is informational - the
 * fold (`ToolCallGroupCard`) remains the place to go digging at raw input/result JSON, and a
 * promoted call still needs no second affordance to say what it did. That is also why it takes no
 * `onClick`: adding one when no caller has a destination for it would be speculative.
 *
 * Not a member of the chip family (`PlanCard`/`AgentChipRow`/`ToolCallGroupCard`, all 36dp glance
 * rows): this is a payload surface with its own geometry ([AuraSpacing.ToolCard]). It shares only
 * their 24dp gutter inset, which it applies ITSELF - callers hand it a plain `fillMaxWidth`
 * modifier, exactly as they do for the chip family, and the alignment stays consistent across
 * every transcript row for free.
 */
@Composable
fun ToolActionCard(
    icon: ImageVector,
    label: String,
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    // surfaceSelected, NOT surfaceInput: the latter is the fill of the composer, every bottom sheet,
    // the attachment tiles AND the 36dp glance-chip family - a card wearing it melts into the chrome
    // around it, which is the exact opposite of a surface whose only reason to exist is to be seen.
    // surfaceSelected is a step lighter and (today) uncontested at this scale. radiusBubble, NOT
    // radiusCard: radiusCard's 20dp was measured for the assist overlay's SMALL floating card and is
    // tight by design (see its KDoc); this card is full-width, the same scale class as a user bubble,
    // and reads visibly squarer than the reference at 20dp.
    Surface(
        color = AuraColors.surfaceSelected,
        shape = RoundedCornerShape(AuraShape.radiusBubble),
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.AssistantText.gutter),
    ) {
        Column(
            modifier = Modifier.padding(
                horizontal = AuraSpacing.Composer.internalPadding,
                vertical = AuraSpacing.ToolCard.paddingVertical,
            ),
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(AuraSpacing.ToolCard.headerIconGap),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Icon(
                    imageVector = icon,
                    contentDescription = null, // the label right beside it already says this
                    tint = AuraColors.textSecondary,
                    modifier = Modifier.size(AuraSpacing.ToolCard.headerIconSize),
                )
                Text(
                    text = label,
                    style = AuraType.chipLabel,
                    color = AuraColors.textSecondary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Spacer(modifier = Modifier.height(AuraSpacing.ToolCard.headerToContentGap))
            content()
        }
    }
}
