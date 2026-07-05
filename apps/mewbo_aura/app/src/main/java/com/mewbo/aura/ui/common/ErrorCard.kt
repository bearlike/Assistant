package com.mewbo.aura.ui.common

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Inline error block (spec §6.12): quiet, no red fills/borders - a warning glyph, a one-line
 * reason, and an optional trailing Retry text-button. Used both for reducer-surfaced failures
 * (`completion.error`/`last_error`) and client-side send failures (data/CLAUDE.md section 6 - there
 * is no dedicated `error` [com.mewbo.aura.data.model.SessionEvent] type, callers always arrive here
 * with an already-resolved human-readable [reason]).
 *
 * [onRetry] `null` renders without the button - the caller's call (`ChatUiState.sessionEnded`),
 * this composable doesn't know why. [retryLabel] defaults to "Retry" for the transcript/search
 * callers; Settings' connection-probe error reuses this same quiet idiom with a "Save anyway"
 * label instead (still a text-button, no new visual language).
 */
@Composable
fun ErrorCard(
    reason: String,
    modifier: Modifier = Modifier,
    retryLabel: String = "Retry",
    onRetry: (() -> Unit)? = null,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.AssistantText.gutter),
    ) {
        Icon(
            imageVector = Icons.Filled.Warning,
            contentDescription = null,
            tint = AuraColors.textSecondary,
            modifier = Modifier.size(WarningGlyphSize),
        )
        Text(
            text = reason,
            style = AuraType.bodyMessage,
            color = AuraColors.textSecondary,
            modifier = Modifier.weight(1f),
        )
        if (onRetry != null) {
            Text(
                text = retryLabel,
                style = AuraType.bodyMessage,
                color = AuraColors.accentPrimary,
                modifier = Modifier
                    .minimumInteractiveComponentSize()
                    .clickable(onClick = onRetry),
            )
        }
    }
}

/** Spec §6.12: "⚠ 20dp glyph" - no icon-size token below [AuraSpacing.Composer.iconSize] (24dp)
 * exists yet; flagged in the task report rather than reusing a size that visibly misses spec. */
private val WarningGlyphSize = 20.dp
