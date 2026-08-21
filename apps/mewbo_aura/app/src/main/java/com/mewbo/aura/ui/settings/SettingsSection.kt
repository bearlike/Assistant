package com.mewbo.aura.ui.settings

import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Stable
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.saveable.Saver
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.style.TextOverflow
import com.mewbo.aura.ui.common.auraFocusRing
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.ui.theme.LocalAssistantExtras

/**
 * Which sections are open, and nothing else.
 *
 * Every section starts closed, so the screen opens quiet and each card reveals on demand. State
 * and the toggling behaviour sit together here rather than as a bare `mutableStateOf` at the call
 * site, which is also what makes [Saver] possible — a rotation or a process death otherwise slams
 * every open card shut under the user.
 */
@Stable
class SectionExpansion(initiallyOpen: List<String> = emptyList()) {
    private val open = mutableStateListOf<String>().apply { addAll(initiallyOpen) }

    fun isOpen(id: String): Boolean = id in open

    fun toggle(id: String) {
        if (!open.remove(id)) open.add(id)
    }

    companion object {
        val Saver: Saver<SectionExpansion, Any> =
            listSaver(save = { it.open.toList() }, restore = { SectionExpansion(it) })
    }
}

/**
 * One collapsible settings section.
 *
 * **Bordered, never filled.** The design language ranks by type, gutters and hairlines rather than
 * by nested filled cards, so the card here is a hairline outline over the canvas. A filled surface
 * would also fight the switches and status glyphs inside it for contrast.
 *
 * [summary] is what makes collapsing safe: a closed card still reports the state underneath it, so
 * folding the screen down hides controls without hiding facts. A section with nothing to report
 * passes `null` and shows only its chevron.
 */
@Composable
fun SettingsSection(
    id: String,
    title: String,
    icon: ImageVector,
    expansion: SectionExpansion,
    modifier: Modifier = Modifier,
    summary: StatusBadge? = null,
    caption: String? = null,
    // Only the screen's very first section passes this — it is how initial D-pad focus lands on
    // the section header rather than nowhere, without every section needing to know it might be first.
    headerFocusRequester: FocusRequester? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    val expanded = expansion.isOpen(id)
    val reducedMotion = LocalAssistantExtras.current.reducedMotion

    Surface(
        color = AuraColors.surfaceCanvas,
        border = BorderStroke(AuraShape.hairlineWidth, AuraColors.outlineHairline),
        shape = RoundedCornerShape(AuraShape.radiusCard),
        modifier = modifier.fillMaxWidth(),
    ) {
        Column(
            modifier = if (reducedMotion) Modifier else Modifier.animateContentSize(tween(AuraMotion.actionRowFadeMs)),
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = AuraSpacing.Settings.rowMinHeight)
                    .then(if (headerFocusRequester != null) Modifier.focusRequester(headerFocusRequester) else Modifier)
                    .auraFocusRing(shape = RoundedCornerShape(AuraShape.radiusCard))
                    .clickable(onClickLabel = if (expanded) "Collapse" else "Expand") { expansion.toggle(id) }
                    .padding(horizontal = AuraSpacing.Composer.internalPadding)
                    .semantics(mergeDescendants = true) {
                        heading()
                        stateDescription = if (expanded) "Expanded" else "Collapsed"
                    },
            ) {
                Icon(
                    imageVector = icon,
                    contentDescription = null,
                    tint = AuraColors.textSecondary,
                    modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
                )
                Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
                // The summary sits UNDER the title, never beside it. A `Row` measures its
                // unweighted children first, so a badge sharing the line claims the width it wants
                // and squeezes the weighted title toward zero — which rendered "Connection and
                // identity" one character per line behind a long failure reason. Stacking removes
                // the competition instead of tuning weights against the longest string anyone
                // might one day put in a badge.
                Column(modifier = Modifier.weight(1f)) {
                    Text(text = title, style = AuraType.sectionHeader, color = AuraColors.textPrimary)
                    if (summary != null && summary.label.isNotBlank()) {
                        Spacer(Modifier.height(AuraSpacing.Settings.captionGap))
                        StatusBadgeText(summary)
                    }
                }
                Spacer(Modifier.width(AuraSpacing.Composer.gapTight))
                Icon(
                    imageVector = Icons.Filled.KeyboardArrowDown,
                    contentDescription = null,
                    tint = AuraColors.textSecondary,
                    modifier = Modifier
                        .size(AuraSpacing.DrawerRow.iconSize)
                        .graphicsLayer(rotationZ = if (expanded) HalfTurnDegrees else 0f),
                )
            }

            if (expanded) {
                HorizontalDivider(color = AuraColors.outlineHairline)
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(bottom = AuraSpacing.Composer.internalPadding),
                ) {
                    if (caption != null) {
                        Text(
                            text = caption,
                            style = AuraType.caption,
                            color = AuraColors.textTertiary,
                            modifier = Modifier.padding(
                                start = AuraSpacing.Composer.internalPadding,
                                end = AuraSpacing.Composer.internalPadding,
                                top = AuraSpacing.Composer.internalPadding,
                            ),
                        )
                    }
                    content()
                }
            }
        }
    }
}

/**
 * One control, its purpose, and its state.
 *
 * [caption] is the whole point of the row shape: a label names a control and says nothing about
 * what it governs, so "Default model" left the user to discover by trying it which sessions it
 * reached. It is a phrase, never a paragraph — anything needing a sentence belongs in the section
 * heading instead.
 */
@Composable
fun SettingsRow(
    label: String,
    modifier: Modifier = Modifier,
    caption: String? = null,
    showChevron: Boolean = false,
    trailing: (@Composable () -> Unit)? = null,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        // The ring LEADS the chain because a tappable row gets its `clickable` from the caller's
        // `modifier`, and a focus observer placed after a focus target never sees it. Leading also
        // rings the row's full bounds rather than the inset the trailing `padding` would leave.
        modifier = Modifier
            .auraFocusRing()
            .then(modifier)
            .fillMaxWidth()
            .heightIn(min = AuraSpacing.Settings.rowMinHeight)
            .padding(horizontal = AuraSpacing.Composer.internalPadding, vertical = AuraSpacing.Composer.gapTight),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary)
            if (caption != null) {
                Spacer(Modifier.height(AuraSpacing.Settings.captionGap))
                Text(text = caption, style = AuraType.caption, color = AuraColors.textSecondary)
            }
        }
        Spacer(Modifier.width(AuraSpacing.Composer.gapTight))
        trailing?.invoke()
        if (showChevron) {
            Icon(
                imageVector = Icons.AutoMirrored.Filled.KeyboardArrowRight,
                contentDescription = null,
                tint = AuraColors.textTertiary,
                modifier = Modifier
                    .padding(start = AuraSpacing.ToolCard.headerIconGap)
                    .size(AuraSpacing.DrawerRow.iconSize),
            )
        }
    }
}

/**
 * A state readout as glyph plus word plus tint, in that order of importance.
 *
 * The glyph is decorative (`contentDescription = null`) because the word beside it already carries
 * the meaning; describing both makes TalkBack announce the state twice.
 */
@Composable
fun StatusBadgeText(badge: StatusBadge, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(AuraSpacing.ToolCard.headerIconGap),
        modifier = modifier,
    ) {
        badge.tone.glyph?.let { glyph ->
            Icon(
                imageVector = glyph,
                contentDescription = null,
                tint = badge.tone.tint,
                modifier = Modifier.size(AuraSpacing.Settings.statusIconSize),
            )
        }
        Text(
            text = badge.label,
            style = AuraType.caption,
            color = badge.tone.tint,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/** A cluster label inside an expanded section, one hierarchy step below its heading. */
@Composable
fun SettingsSubheader(text: String, icon: ImageVector, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
        modifier = modifier
            .fillMaxWidth()
            .padding(
                start = AuraSpacing.Composer.internalPadding,
                end = AuraSpacing.Composer.internalPadding,
                top = AuraSpacing.Composer.internalPadding,
                bottom = AuraSpacing.Settings.captionGap,
            )
            .semantics { heading() },
    ) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            tint = AuraColors.textTertiary,
            modifier = Modifier.size(AuraSpacing.Settings.statusIconSize),
        )
        Text(text = text, style = AuraType.caption, color = AuraColors.textSecondary)
    }
}

private const val HalfTurnDegrees = 180f
