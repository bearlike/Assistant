package com.mewbo.aura.ui.settings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Default-project picker - mirrors [com.mewbo.aura.ui.chat.ModelPickerSheet]'s
 * anatomy (`surfaceInput` container, `radiusBubble` top corners, 56dp rows, trailing
 * `accentPrimary` check on selected). First row is always "Temporary directory" (empty
 * [selectedKey]).
 *
 * `projects == null` (offline/error, [SettingsViewModel.loadProjectsIfNeeded] already fired its own
 * notice before this ever opens) degrades to a single current-selection row, same shape as
 * [com.mewbo.aura.ui.chat.ModelPickerSheet]'s own offline degrade - never crashes.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProjectPickerSheet(
    projects: List<ProjectSummary>?,
    selectedKey: String,
    onSelect: (String) -> Unit,
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
        if (projects == null) {
            ProjectPickerRow(
                label = selectedKey.ifBlank { "Temporary directory" },
                temporary = selectedKey.isBlank(),
                selected = true,
                onClick = onDismiss,
            )
            return@ModalBottomSheet
        }

        Column(modifier = Modifier.fillMaxWidth()) {
            ProjectPickerRow(label = "Temporary directory", temporary = true, selected = selectedKey.isBlank(), onClick = { onSelect("") })
            // The ephemeral temp-dir cwd is set apart from the real, saved projects below by a divider
            // (user directive 2026-07-14): it is a throwaway scratch context, not one of your projects.
            HorizontalDivider(color = AuraColors.outlineHairline)
            projects.forEach { project ->
                ProjectPickerRow(
                    label = project.name,
                    temporary = false,
                    selected = project.contextKey == selectedKey,
                    onClick = { onSelect(project.contextKey) },
                )
            }
        }
    }
}

@Composable
private fun ProjectPickerRow(label: String, temporary: Boolean, selected: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        // Leading scope glyph: the ephemeral "date_range" for the temporary scratch cwd (muted tint),
        // the "project" glyph (scopeProject accent) for a real project. Filled slot on every row, so
        // this is NOT the §7.13 empty-icon-slot indent regression (that was an EMPTY reserved box).
        Icon(
            imageVector = if (temporary) ChatIcons.TemporaryProjectScope else ChatIcons.ProjectScope,
            contentDescription = null,
            tint = if (temporary) AuraColors.textSecondary else AuraColors.scopeProject,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(modifier = Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary, modifier = Modifier.weight(1f))
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

/** Top corners only (same shape [com.mewbo.aura.ui.chat.ModelPickerSheet] uses). */
private val SheetShape = RoundedCornerShape(topStart = AuraShape.radiusBubble, topEnd = AuraShape.radiusBubble)
