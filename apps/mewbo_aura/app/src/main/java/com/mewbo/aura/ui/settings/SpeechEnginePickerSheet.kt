package com.mewbo.aura.ui.settings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.model.SpeechDirection
import com.mewbo.aura.ui.common.AuraListBottomSheet
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Picks the engine for ONE speech direction.
 *
 * Its own sheet rather than a reuse of `ModelPickerSheet`, which is welded to [
 * com.mewbo.aura.data.model.ModelCatalog] and its popular/more partitioning — speech engines are a
 * different namespace with a different shape and no notion of a pinned family. The CONTAINER is
 * still the shared [AuraListBottomSheet], so nothing about bounding, scrolling or insets is
 * re-derived here (`ui/common/CLAUDE.md`: the container is never the thing you fork).
 *
 * **On device is always the first row and is always present**, even when the catalog failed to
 * load — it needs no server, so there is no state in which it should be unofferable. That is also
 * what makes a failed fetch a soft failure: the sheet still does something useful.
 *
 * **Every server row is marked, twice.** A cloud glyph in the leading slot and
 * [SpeechCatalog.CLOUD_MARK] inside the label itself. Two signals rather than one for the reason
 * this screen's whole design rests on (`ui/settings/CLAUDE.md`): colour or a lone glyph is not
 * enough, and the fact being reported here — that audio leaves the device — is the one a user is
 * least able to discover by trying it.
 */
@Composable
fun SpeechEnginePickerSheet(
    catalog: SpeechCatalog?,
    direction: SpeechDirection,
    selectedId: String,
    onSelect: (String) -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val serverOptions = catalog?.serverOptions(direction).orEmpty()

    AuraListBottomSheet(onDismiss = onDismiss, modifier = modifier) {
        item(key = OnDeviceRowKey) {
            EngineRow(
                label = SpeechCatalog.ON_DEVICE_LABEL,
                caption = onDeviceCaption(direction),
                glyph = Icons.Filled.PhoneAndroid,
                selected = SpeechCatalog.isOnDevice(selectedId),
                onClick = { onSelect(SpeechCatalog.ON_DEVICE) },
            )
        }
        if (serverOptions.isNotEmpty()) {
            // The divider is the boundary between "stays on this phone" and "does not" — the same
            // job the one under Temporary does in `ProjectPickerSheet`.
            item(key = DividerKey) {
                HorizontalDivider(color = AuraColors.outlineHairline)
            }
            items(serverOptions, key = { it.id }) { option ->
                EngineRow(
                    label = SpeechCatalog.cloudLabel(option.label),
                    caption = option.id,
                    glyph = Icons.Filled.Cloud,
                    selected = option.id == selectedId,
                    onClick = { onSelect(option.id) },
                )
            }
        }
    }
}

/** Names the CONSEQUENCE, not the mechanism — the caption convention this screen already follows
 * for "Display over other apps". */
private fun onDeviceCaption(direction: SpeechDirection): String = when (direction) {
    SpeechDirection.SpeechToText -> "Your voice never leaves this phone"
    SpeechDirection.TextToSpeech -> "Replies are spoken by this phone"
}

private const val OnDeviceRowKey = "speech-engine-on-device"
private const val DividerKey = "speech-engine-divider"

@Composable
private fun EngineRow(
    label: String,
    caption: String,
    glyph: ImageVector,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Icon(
            imageVector = glyph,
            contentDescription = null,
            tint = AuraColors.iconPrimary,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
            Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary)
            Text(text = caption, style = AuraType.caption, color = AuraColors.textSecondary)
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
