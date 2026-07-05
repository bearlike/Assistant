package com.mewbo.aura.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.mewbo.aura.data.model.ModelCatalog
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Model picker (task brief W1-A): [ModelCatalog.popular]'s pinned rows always visible, everything
 * else ([ModelCatalog.more]) behind an expanding "More models" row (spec anatomy per the reference
 * plus-menu/tools-sheet captures: drag handle, ~56dp rows, icon/label rhythm - reused here as
 * [AuraSpacing.DrawerRow] tokens rather than inventing new ones).
 *
 * `models == null` (offline/error - [ChatScreen] already fired the "Couldn't load models" notice
 * via [ChatViewModel.loadModelsIfNeeded] before this ever opens) degrades to a single
 * current-selection row instead of a list; never crashes.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ModelPickerSheet(
    models: ModelCatalog?,
    selectedModel: String?,
    onSelect: (String?) -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(),
        containerColor = AuraColors.surfaceInput,
        shape = SheetShape,
        modifier = modifier,
    ) {
        if (models == null) {
            ModelRow(
                id = selectedModel ?: "Core",
                label = selectedModel?.let(ModelCatalog::normalize) ?: "Core",
                selected = true,
                onClick = onDismiss,
            )
            return@ModalBottomSheet
        }

        var moreExpanded by remember { mutableStateOf(false) }
        val more = models.more()

        Column(modifier = Modifier.fillMaxWidth()) {
            models.popular().forEach { id ->
                ModelRow(
                    id = id,
                    label = models.displayName(id),
                    selected = models.isSelected(id, selectedModel),
                    onClick = { onSelect(id) },
                )
            }

            if (more.isNotEmpty()) {
                MoreModelsRow(expanded = moreExpanded, onClick = { moreExpanded = !moreExpanded })
                AnimatedVisibility(visible = moreExpanded) {
                    Column {
                        more.forEach { id ->
                            ModelRow(
                                id = id,
                                label = models.displayName(id),
                                selected = models.isSelected(id, selectedModel),
                                onClick = { onSelect(id) },
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ModelRow(id: String, label: String, selected: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
            Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary)
            Text(text = id, style = AuraType.caption, color = AuraColors.textSecondary)
        }
        if (selected) {
            Icon(
                imageVector = Icons.Filled.Check,
                contentDescription = "Selected",
                tint = AuraColors.accentPrimary,
                modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
            )
        }
    }
}

@Composable
private fun MoreModelsRow(expanded: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.DrawerRow.sectionHeaderTopPad / 2),
    ) {
        Text(text = if (expanded) "Fewer models" else "More models", style = AuraType.sectionHeader)
    }
}

/** Top corners only, at [AuraShape.radiusBubble] (task brief) - `AuraShape` has no
 * stadium-minus-bottom-corners token, so this composes the existing radius directly rather than
 * adding a one-call-site shape to the shared theme object. */
private val SheetShape = RoundedCornerShape(topStart = AuraShape.radiusBubble, topEnd = AuraShape.radiusBubble)
