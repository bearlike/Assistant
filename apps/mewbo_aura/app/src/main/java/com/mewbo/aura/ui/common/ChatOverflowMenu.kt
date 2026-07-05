package com.mewbo.aura.ui.common

import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/** One row in a [ChatOverflowMenu]. */
data class OverflowMenuItem(val icon: ImageVector, val label: String, val onClick: () -> Unit)

/**
 * The one overflow-menu container style (Rev E §E-5, frame F4): `surfaceSelected` fill, ~20dp
 * radius, 24dp leading icon + `listItem` text per row. Shared by the per-message ⋯ (spec §6.5, one
 * working item: "Select text") and the top bar's in-chat ⋯ (spec §6.1/Rev E-5, one working item:
 * "Copy conversation") - v1 ships only what works, no disabled stub rows.
 */
@Composable
fun ChatOverflowMenu(
    expanded: Boolean,
    onDismiss: () -> Unit,
    items: List<OverflowMenuItem>,
    modifier: Modifier = Modifier,
) {
    DropdownMenu(
        expanded = expanded,
        onDismissRequest = onDismiss,
        containerColor = AuraColors.surfaceSelected,
        shape = RoundedCornerShape(MenuCornerRadius),
        modifier = modifier,
    ) {
        items.forEach { item ->
            DropdownMenuItem(
                text = { Text(text = item.label, style = AuraType.listItem, color = AuraColors.textPrimary) },
                leadingIcon = {
                    Icon(
                        imageVector = item.icon,
                        contentDescription = null,
                        tint = AuraColors.iconPrimary,
                        modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
                    )
                },
                onClick = item.onClick,
            )
        }
    }
}

/** Rev E-5: "~20dp radius" - no matching [AuraSpacing]/`AuraShape` token yet (16dp `radiusThumb`
 * reads visibly tighter, 28dp `radiusBubble` visibly looser); flagged in the task report. */
private val MenuCornerRadius = 20.dp
