package com.mewbo.aura.ui.composer

import android.net.Uri
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Icon
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.StagedAttachment
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.common.AttachmentGlyphs
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * The composer (spec §6.2, Rev E §E-3 frame corrections), one component, five states
 * ([ComposerState] C1-C4 + [ComposerStyle] C5), consumed by both the chat screen (W2-A) and the
 * assist overlay (W3). Dumb by design - it only renders whatever [state] it's handed
 * ([ComposerState.resolve] is where the derivation logic lives) and reports intent via callbacks;
 * it owns no session/run/dictation state itself.
 *
 * Anatomy (Rev E §E-3, ground-truthed against frames F1/F3/F6 - overrides the original brief's
 * "leading + hidden" / "circle" reading):
 * - Leading "+" is always visible (`onAttachTap`; v1 host behavior is the caller's call, e.g. a
 *   "coming soon" notice - not this component's concern).
 * - C1 idle trailing = a mic affordance (`onMicTap`, dictation into the field) + a separate 44dp
 *   rounded-square tile (`onVoiceModeTap`, a full voice turn - distinct from dictation). The mic's
 *   own anatomy is [style]-dependent (frame F1 vs. F3): a bare glyph when [ComposerStyle.Docked],
 *   a filled light-blue circle when [ComposerStyle.FloatingOverlay].
 * - C2/C3/C4 trailing collapses to the single morphing action circle (send/stop); C3 additionally
 *   shows an outlined stop tile (`onDictationStop`) in the mic glyph's old position.
 * - [trailingAccessory], when supplied, replaces the ENTIRE trailing cluster (not just the circle)
 *   - W3 needs the whole slot for its own mic-circle + waveform-tile pairing (Rev E §E-3 C5).
 *
 * Morphs (M5, spec §7): state changes animate the trailing circle's color+glyph and crossfade the
 * center content - never a hard swap. Both specs come from the theme: [AuraMotion.composerMorphSpring]
 * (Float, content crossfades) and [AuraMotion.composerMorphColorSpring] (its `Color`-typed sibling,
 * circle fill) - `spring(...)` construction stays reserved to `ui/theme/`.
 *
 * @param onMicTap tap the bare mic glyph (C1) - starts dictation ([ComposerState.Dictation]).
 * @param onDictationStop tap the C3-only stop tile - ends dictation without sending.
 * @param onVoiceModeTap tap the C1 rounded-square tile - starts a full voice turn (this app's
 *   existing `AssistTurnMachine.startListening()` path, which tags the turn `InputModality.Voice`
 *   via `beginTurn` - distinct from dictation-into-field).
 * @param micEnabled independent gate for JUST the C1 mic glyph (the "Unavailable ->
 *   disabled mic" ruling - the device's on-device recognizer capability, distinct from [enabled]'s
 *   broader offline/session-ended gating). Defaults to [enabled] so every pre-existing caller
 *   (the assist overlay never passes this) keeps its old all-or-nothing behavior unchanged.
 * @param onAttachTap tap the leading "+" - opens the composer options sheet (the
 *   docked chat host wires this, the assist overlay never supplies it and keeps the pre-W2 no-op).
 * @param style [ComposerStyle.FloatingOverlay] is the assist-overlay's floating pill (Rev D-1);
 *   pass a non-null [trailingAccessory] there to host the orb + overlay's own trailing cluster.
 * @param stagedAttachments files picked in the options sheet but not yet sent - rendered as a chip
 *   row above the input, growing the pill's own shape from [AuraShape.radiusPill]
 *   to [AuraShape.radiusBubble] the same way a wrapped multi-line draft does
 *   ([ComposerTextField]'s own `onTextLayout` drives that second trigger). Chips are disabled (not
 *   hidden) while [state] is [ComposerState.Streaming] - a steer can't carry them (RunRepository).
 * @param showDragHandle [R4 2026-07-10] the assist overlay's ONLY caller advertises its existing
 *   pill-pull-up-to-app gesture ([ui/overlay/CLAUDE.md] "Pill pull-up") with a small top-edge notch,
 *   reusing the response card's own handle tokens verbatim - one handle vocabulary, not a second
 *   size. Hidden while [expanded] (a grown pill has no stable top-center resting spot). Defaults
 *   `false` so the docked composer (its own caller never passes this) stays byte-identical.
 */
@Composable
fun AuraComposer(
    state: ComposerState,
    draft: TextFieldValue,
    onDraftChange: (TextFieldValue) -> Unit,
    onSend: () -> Unit,
    onStop: () -> Unit,
    onMicTap: () -> Unit,
    onDictationStop: () -> Unit,
    onVoiceModeTap: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    micEnabled: Boolean = enabled,
    onAttachTap: () -> Unit = {},
    style: ComposerStyle = ComposerStyle.Docked,
    trailingAccessory: (@Composable () -> Unit)? = null,
    stagedAttachments: List<StagedAttachment> = emptyList(),
    showDragHandle: Boolean = false,
    onRemoveAttachment: (Uri) -> Unit = {},
) {
    val isDictating = state is ComposerState.Dictation
    val dictationRms = (state as? ComposerState.Dictation)?.rmsDb ?: 0f
    val dictationPartialText = (state as? ComposerState.Dictation)?.partialText
    val hasChips = stagedAttachments.isNotEmpty()
    // a wrapped (2+ line) draft grows the pill the same way staged chips already
    // do - ComposerTextField's onTextLayout is the only source for this (BasicTextField's own
    // reported TextLayoutResult.lineCount), never derived from character count.
    var isMultiline by remember { mutableStateOf(false) }
    val expanded = hasChips || isMultiline

    Surface(
        color = style.pillColor,
        shape = if (expanded) RoundedCornerShape(AuraShape.radiusBubble) else AuraShape.radiusPill,
        modifier = modifier.then(if (expanded) Modifier else Modifier.height(style.pillHeight)),
    ) {
        Box {
            Column {
                if (hasChips) {
                    AttachmentChipRow(
                        attachments = stagedAttachments,
                        enabled = enabled && state !is ComposerState.Streaming,
                        onRemove = onRemoveAttachment,
                        modifier = Modifier.padding(
                            start = AuraSpacing.Composer.internalPadding,
                            end = AuraSpacing.Composer.internalPadding,
                            top = AuraSpacing.Composer.internalPadding,
                        ),
                    )
                }

                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(ComposerTightGap),
                    modifier = Modifier
                        .fillMaxWidth()
                        .then(if (expanded) Modifier else Modifier.height(style.pillHeight))
                        .padding(horizontal = AuraSpacing.Composer.internalPadding),
                ) {
                    ComposerBareIconButton(
                        icon = Icons.Filled.Add,
                        description = "Attach",
                        enabled = enabled,
                        onClick = onAttachTap,
                    )

                    AnimatedContent(
                        targetState = isDictating,
                        transitionSpec = { ComposerMorphTransform },
                        label = "composer-center-content",
                        modifier = Modifier.weight(1f),
                    ) { dictating ->
                        if (dictating) {
                            DictationCenterContent(rmsDb = dictationRms, partialText = dictationPartialText)
                        } else {
                            ComposerTextField(
                                draft = draft,
                                onDraftChange = onDraftChange,
                                enabled = enabled,
                                onLineCountChange = { lineCount -> isMultiline = lineCount > 1 },
                                textStyle = style.fieldTextStyle,
                            )
                        }
                    }

                    if (trailingAccessory != null) {
                        trailingAccessory()
                    } else {
                        ComposerTrailingCluster(
                            state = state,
                            style = style,
                            enabled = enabled,
                            micEnabled = micEnabled,
                            onSend = onSend,
                            onStop = onStop,
                            onMicTap = onMicTap,
                            onDictationStop = onDictationStop,
                            onVoiceModeTap = onVoiceModeTap,
                        )
                    }
                }
            }

            // [R4 2026-07-10] expansion hint: advertises the overlay pill's existing swipe-up
            // pull-to-app gesture (ui/overlay/CLAUDE.md "Pill pull-up"). Reuses the response
            // card's handle vocabulary/tokens verbatim - one handle language, not a second size.
            if (showDragHandle && !expanded) {
                Box(
                    modifier = Modifier
                        .align(Alignment.TopCenter)
                        .padding(top = AuraSpacing.ResponseCard.dragHandleTopOffset)
                        .size(
                            width = AuraSpacing.ResponseCard.dragHandleWidth,
                            height = AuraSpacing.ResponseCard.dragHandleHeight,
                        )
                        .background(color = AuraColors.textSecondary, shape = AuraShape.radiusPill),
                )
            }
        }
    }
}

/** Horizontally-scrolling row of [AttachmentChip]s above the composer's input row. */
@Composable
private fun AttachmentChipRow(
    attachments: List<StagedAttachment>,
    enabled: Boolean,
    onRemove: (Uri) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        horizontalArrangement = Arrangement.spacedBy(ComposerTightGap),
        modifier = modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState()),
    ) {
        attachments.forEach { attachment ->
            AttachmentChip(attachment = attachment, enabled = enabled, onRemove = { onRemove(attachment.uri) })
        }
    }
}

@Composable
private fun AttachmentChip(attachment: StagedAttachment, enabled: Boolean, onRemove: () -> Unit, modifier: Modifier = Modifier) {
    Surface(color = AuraColors.surfaceSelected, shape = AuraShape.radiusPill, modifier = modifier) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(AttachmentChipInnerGap),
            modifier = Modifier.padding(horizontal = ComposerTightGap, vertical = AttachmentChipVerticalPadding),
        ) {
            Icon(
                imageVector = AttachmentGlyphs.forMimeType(attachment.mimeType),
                contentDescription = null,
                tint = AuraColors.textSecondary,
                modifier = Modifier.size(AttachmentChipIconSize),
            )
            Text(text = attachment.displayName, style = AuraType.chipLabel, color = AuraColors.textPrimary, maxLines = 1)
            Icon(
                imageVector = Icons.Filled.Close,
                contentDescription = "Remove ${attachment.displayName}",
                tint = AuraColors.textSecondary,
                modifier = Modifier
                    .size(AttachmentChipIconSize)
                    .clickable(enabled = enabled, onClick = onRemove),
            )
        }
    }
}

/** M5's single 200ms crossfade, shared by every morph site in this file (center content, trailing
 * slots, circle glyph) - "one cluster, not per-state composables that pop" (spec §6.2 Morphs). */
private val ComposerMorphTransform =
    fadeIn(AuraMotion.composerMorphSpring) togetherWith fadeOut(AuraMotion.composerMorphSpring)

/** Spec §6.2 C5 / Rev E frame F3: the overlay's floating pill is a distinct navy fill. */
private val ComposerStyle.pillColor: Color
    get() = when (this) {
        ComposerStyle.Docked -> AuraColors.surfaceInput
        ComposerStyle.FloatingOverlay -> AuraColors.surfaceOverlayPill
    }

/** [R4 2026-07-10] style-forked pill height — see AuraSpacing.Composer.overlayHeight's KDoc. */
private val ComposerStyle.pillHeight: Dp
    get() = when (this) {
        ComposerStyle.Docked -> AuraSpacing.Composer.height
        ComposerStyle.FloatingOverlay -> AuraSpacing.Composer.overlayHeight
    }

/** [R4 2026-07-10] style-forked trailing circle/tile size. */
private val ComposerStyle.actionCircleSize: Dp
    get() = when (this) {
        ComposerStyle.Docked -> AuraSpacing.Composer.actionCircleSize
        ComposerStyle.FloatingOverlay -> AuraSpacing.Composer.overlayActionCircleSize
    }

/** [R4 2026-07-10] style-forked field/invitation type scale. */
private val ComposerStyle.fieldTextStyle: TextStyle
    get() = when (this) {
        ComposerStyle.Docked -> AuraType.bodyMessage
        ComposerStyle.FloatingOverlay -> AuraType.composerOverlay
    }

/** Gap between adjacent composer elements (leading "+", center content, trailing cluster, and the
 * cluster's own two slots). */
private val ComposerTightGap: Dp = AuraSpacing.Composer.gapTight

/** Cap for [ComposerTextField]'s wrap (task brief) - internal scroll takes over
 * beyond this, same as any capped multi-line field. */
private const val ComposerMaxLines = 6

/** No matching [AuraSpacing] tokens for the attachment chip's inner glyph size / gap / vertical
 * padding; flagged in the task report (same convention `ui/chat/ChatScreen.kt`'s `TitleGap`/
 * `ChevronSize` already established for a genuinely missing token). */
private val AttachmentChipIconSize: Dp = 16.dp
private val AttachmentChipInnerGap: Dp = AuraSpacing.Composer.gapTight / 2
private val AttachmentChipVerticalPadding: Dp = AuraSpacing.Composer.gapTight / 2

/** A bare, container-less icon glyph with a touch-target floor - the leading "+" and C1's mic
 * glyph both use this (Rev E §E-3: "bare mic glyph", no circle). */
@Composable
private fun ComposerBareIconButton(
    icon: ImageVector,
    description: String,
    enabled: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .minimumInteractiveComponentSize()
            .clickable(enabled = enabled, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            imageVector = icon,
            contentDescription = description,
            tint = AuraColors.iconPrimary,
            modifier = Modifier.size(AuraSpacing.Composer.iconSize),
        )
    }
}

/** Rev E §E-3 C5 (frame F3): the overlay's mic slot is a filled light-blue circle with a dark
 * glyph, not the docked composer's bare icon - also the eventual orb-dock position (D-1), though
 * that dock is the caller's `trailingAccessory`, not this composable. Internal (not private): the
 * Done-state trailing cluster in `ui/overlay/AssistOverlayScreen.kt` reuses this exact same circle
 * for the post-turn voice-follow-up mic (fix-round-3 Important #2) rather than hand-rolling a
 * second one - `trailingAccessory` replaces the WHOLE cluster there, so it can't reach
 * [ComposerTrailingCluster]'s own copy of this composable. */
@Composable
internal fun ComposerOverlayMicCircle(
    enabled: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    size: Dp = AuraSpacing.Composer.actionCircleSize,
) {
    Surface(
        onClick = onClick,
        enabled = enabled,
        shape = AuraShape.radiusPill,
        color = AuraColors.accentOverlayMic,
        modifier = modifier
            .minimumInteractiveComponentSize()
            .size(size),
    ) {
        Box(contentAlignment = Alignment.Center) {
            Icon(
                imageVector = ChatIcons.Mic,
                contentDescription = "Voice input",
                tint = AuraColors.onAccentOverlayMic,
                modifier = Modifier.size(AuraSpacing.Composer.iconSize),
            )
        }
    }
}

/** [onLineCountChange] fires on every layout pass ([androidx.compose.foundation.text.BasicTextField]'s
 * own `onTextLayout`) - [AuraComposer] uses it (not character count) to decide when a wrapped draft
 * should grow the pill. */
@Composable
private fun ComposerTextField(
    draft: TextFieldValue,
    onDraftChange: (TextFieldValue) -> Unit,
    enabled: Boolean,
    onLineCountChange: (Int) -> Unit,
    textStyle: TextStyle,
    modifier: Modifier = Modifier,
) {
    BasicTextField(
        value = draft,
        onValueChange = onDraftChange,
        enabled = enabled,
        singleLine = false,
        maxLines = ComposerMaxLines,
        onTextLayout = { layoutResult -> onLineCountChange(layoutResult.lineCount) },
        textStyle = textStyle.copy(color = AuraColors.textPrimary),
        cursorBrush = SolidColor(AuraColors.accentPrimary),
        modifier = modifier,
        decorationBox = { innerTextField ->
            Box(contentAlignment = Alignment.CenterStart) {
                if (draft.text.isEmpty()) {
                    Text(text = "Ask Mewbo", style = textStyle.copy(color = AuraColors.textSecondary))
                }
                innerTextField()
            }
        },
    )
}

/** C3's center content (spec §6.2): live bars, or the finalizing transcript once words settle. */
@Composable
private fun DictationCenterContent(rmsDb: Float, partialText: String?, modifier: Modifier = Modifier) {
    Box(modifier = modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        if (partialText != null) {
            Text(
                text = partialText,
                style = AuraType.bodyMessage.copy(color = AuraColors.textPrimary),
                textAlign = TextAlign.Center,
                maxLines = 1,
            )
        } else {
            RmsWaveform(rmsDb = rmsDb)
        }
    }
}

/** Which element occupies the trailing cluster's left slot (Rev E §E-3: the bare mic glyph and
 * the C3 stop tile share this one position - "mic hides" is really "this slot goes empty"). */
private enum class TrailingLeftSlot { Mic, StopTile, None }

@Composable
private fun ComposerTrailingCluster(
    state: ComposerState,
    style: ComposerStyle,
    enabled: Boolean,
    micEnabled: Boolean,
    onSend: () -> Unit,
    onStop: () -> Unit,
    onMicTap: () -> Unit,
    onDictationStop: () -> Unit,
    onVoiceModeTap: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier,
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(ComposerTightGap),
    ) {
        val leftSlot = when (state) {
            is ComposerState.Idle -> TrailingLeftSlot.Mic
            is ComposerState.Dictation -> TrailingLeftSlot.StopTile
            else -> TrailingLeftSlot.None
        }
        AnimatedContent(
            targetState = leftSlot,
            transitionSpec = { ComposerMorphTransform },
            label = "composer-trailing-left-slot",
        ) { slot ->
            when (slot) {
                // Rev E §E-3: the mic slot's own anatomy differs by style - Docked keeps the bare
                // glyph, but the overlay's C5 anatomy (frame F3) upgrades it to a filled light-blue
                // circle (also where D-1's orb eventually docks, via the caller's trailingAccessory).
                TrailingLeftSlot.Mic -> when (style) {
                    ComposerStyle.Docked ->
                        ComposerBareIconButton(icon = ChatIcons.Mic, description = "Dictate", enabled = micEnabled, onClick = onMicTap)
                    ComposerStyle.FloatingOverlay ->
                        ComposerOverlayMicCircle(enabled = micEnabled, onClick = onMicTap, size = style.actionCircleSize)
                }
                TrailingLeftSlot.StopTile -> ComposerStopTile(enabled = enabled, onClick = onDictationStop, size = style.actionCircleSize)
                // No size modifier - an empty, childless Box has zero intrinsic size on its own.
                TrailingLeftSlot.None -> Box(Modifier)
            }
        }

        // Primary slot: C1's rounded-square voice-mode tile, or the morphing action circle
        // everywhere else. ComposerActionCircle stays defensively exhaustive over Idle too (see its
        // own doc) so a stale frame mid-crossfade can never crash.
        AnimatedContent(
            targetState = state is ComposerState.Idle,
            transitionSpec = { ComposerMorphTransform },
            label = "composer-trailing-primary-slot",
        ) { isIdle ->
            if (isIdle) {
                ComposerVoiceModeTile(enabled = enabled, onClick = onVoiceModeTap, size = style.actionCircleSize)
            } else {
                ComposerActionCircle(
                    state = state,
                    enabled = enabled,
                    onSend = onSend,
                    onStop = onStop,
                    onVoiceModeTap = onVoiceModeTap,
                    size = style.actionCircleSize,
                )
            }
        }
    }
}

/** The one morphing element (M5): container color + glyph both retarget per [state] through
 * [ComposerMorphTransform]/[AuraMotion.composerMorphColorSpring], never popping between per-state
 * composables. */
private data class ActionSpec(
    val glyph: ImageVector,
    val description: String,
    val containerColor: Color,
    val onClick: () -> Unit,
)

@Composable
private fun ComposerActionCircle(
    state: ComposerState,
    enabled: Boolean,
    onSend: () -> Unit,
    onStop: () -> Unit,
    onVoiceModeTap: () -> Unit,
    modifier: Modifier = Modifier,
    size: Dp = AuraSpacing.Composer.actionCircleSize,
) {
    val spec = when (state) {
        // Defensive only - ComposerTrailingCluster renders ComposerVoiceModeTile instead during
        // the steady Idle state; this branch exists purely so a stale frame mid-AnimatedContent
        // transition (rapid Idle<->Typing toggling) can never crash on a non-exhaustive `when`.
        is ComposerState.Idle -> ActionSpec(ChatIcons.Waveform, "Voice mode", AuraColors.accentMuted, onVoiceModeTap)
        is ComposerState.Typing -> ActionSpec(ChatIcons.UpArrow, "Send", AuraColors.accentPrimary, onSend)
        is ComposerState.Dictation -> ActionSpec(ChatIcons.UpArrow, "Send", AuraColors.accentPrimary, onSend)
        is ComposerState.Streaming -> if (state.hasDraft) {
            ActionSpec(ChatIcons.UpArrow, "Send", AuraColors.accentPrimary, onSend)
        } else {
            ActionSpec(ChatIcons.Stop, "Stop", AuraColors.accentMuted, onStop)
        }
    }

    val animatedColor by animateColorAsState(
        targetValue = spec.containerColor,
        animationSpec = AuraMotion.composerMorphColorSpring,
        label = "composer-circle-color",
    )

    Surface(
        onClick = spec.onClick,
        enabled = enabled,
        shape = AuraShape.radiusPill,
        color = animatedColor,
        // Visual circle is the spec's 44dp; minimumInteractiveComponentSize expands only the touch
        // target to the 48dp floor (ui/CLAUDE.md accessibility) without growing the painted pill.
        modifier = modifier
            .minimumInteractiveComponentSize()
            .size(size),
    ) {
        Box(contentAlignment = Alignment.Center) {
            AnimatedContent(
                targetState = spec.glyph,
                transitionSpec = { ComposerMorphTransform },
                label = "composer-circle-glyph",
            ) { glyph ->
                Icon(
                    imageVector = glyph,
                    contentDescription = spec.description,
                    tint = AuraColors.accentOnAccent,
                    modifier = Modifier.size(AuraSpacing.Composer.iconSize),
                )
            }
        }
    }
}

/** C1's voice-mode entry (Rev E §E-3): a rounded-square tile, NOT a circle - distinct from
 * [ComposerActionCircle] so the C1<->C2 transition genuinely changes shape, not just color/glyph. */
@Composable
private fun ComposerVoiceModeTile(
    enabled: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    size: Dp = AuraSpacing.Composer.actionCircleSize,
) {
    Surface(
        onClick = onClick,
        enabled = enabled,
        shape = RoundedCornerShape(AuraShape.radiusThumb),
        color = AuraColors.accentMuted,
        modifier = modifier
            .minimumInteractiveComponentSize()
            .size(size),
    ) {
        Box(contentAlignment = Alignment.Center) {
            Icon(
                imageVector = ChatIcons.Waveform,
                contentDescription = "Voice mode",
                tint = AuraColors.accentOnAccent,
                modifier = Modifier.size(AuraSpacing.Composer.iconSize),
            )
        }
    }
}

@Composable
private fun ComposerStopTile(
    enabled: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    size: Dp = AuraSpacing.Composer.actionCircleSize,
) {
    Surface(
        onClick = onClick,
        enabled = enabled,
        shape = RoundedCornerShape(AuraShape.radiusThumb),
        // Rev E §E-3: "outlined" tile - the outline is baked into ChatIcons.StopTile's stroked
        // path, so this container stays transparent (touch target + ripple only).
        color = Color.Transparent,
        modifier = modifier
            .minimumInteractiveComponentSize()
            .size(size),
    ) {
        Box(contentAlignment = Alignment.Center) {
            Icon(
                imageVector = ChatIcons.StopTile,
                contentDescription = "Stop dictation",
                tint = AuraColors.iconPrimary,
                modifier = Modifier.size(AuraSpacing.Composer.iconSize),
            )
        }
    }
}
