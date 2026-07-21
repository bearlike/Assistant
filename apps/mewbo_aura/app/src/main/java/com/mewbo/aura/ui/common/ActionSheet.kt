package com.mewbo.aura.ui.common

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
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
 * The chrome every long-press action sheet in the app shares: the top-corners-only shape, a
 * non-interactive title header, a 56dp glyph+label action row, and the one-line inline failure
 * caption. Extracted here once a SECOND sheet needed the identical pieces
 * ([com.mewbo.aura.ui.chat.MessageActionsSheet], the transcript's user-message actions) rather than
 * copied - [com.mewbo.aura.ui.navigation.SessionActionsSheet] (the drawer's Recents-row actions) is
 * where all four originated and it now consumes them from here unchanged.
 *
 * The `ModalBottomSheet` itself is deliberately NOT wrapped: the two sheets differ in what they hold
 * (a pane machine vs. a flat row list) and in nothing else, so a shared container would only be a
 * lambda pass-through. Chrome is shared; structure stays each sheet's own.
 */

/** Top corners only, same as `ComposerOptionsSheet`/`ModelPickerSheet`. */
val SheetShape = RoundedCornerShape(topStart = AuraShape.radiusBubble, topEnd = AuraShape.radiusBubble)

/**
 * Non-interactive title header (caption/secondary treatment, the same tier `ComposerOptionsSheet`'s
 * "Session" section label uses). The CALLER resolves the string - a session's title with its
 * "Untitled session" fallback, or a message's own text - so this stays a pure presentation shell
 * with no per-domain knowledge in it.
 */
@Composable
fun SheetHeader(text: String, modifier: Modifier = Modifier, maxLines: Int = 2) {
    Text(
        text = text,
        style = AuraType.sectionHeader,
        maxLines = maxLines,
        overflow = TextOverflow.Ellipsis,
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.DrawerRow.sectionHeaderTopPad / 2),
    )
}

/** One tappable action (glyph + label) in a sheet's root pane - the 56dp `DrawerRow.height` row the
 * whole sheet family (`ComposerOptionsSheet`/`ModelPickerSheet`) already uses. [enabled] is what an
 * in-flight action greys the OTHER rows out with. */
@Composable
fun SheetActionRow(label: String, icon: ImageVector, enabled: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            tint = if (enabled) AuraColors.iconPrimary else AuraColors.textTertiary,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Text(
            text = label,
            style = AuraType.listItem,
            color = if (enabled) AuraColors.textPrimary else AuraColors.textTertiary,
            modifier = Modifier.weight(1f),
        )
    }
}

/** A failed action's one-line reason, in place, with the sheet staying OPEN so the action is simply
 * retryable - never a toast on top of the sheet that raised it (DESIGN.md §6: no error residue). */
@Composable
fun SheetErrorCaption(text: String, modifier: Modifier = Modifier) {
    Text(
        text = text,
        style = AuraType.caption,
        color = AuraColors.accentError,
        modifier = modifier.padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight),
    )
}
