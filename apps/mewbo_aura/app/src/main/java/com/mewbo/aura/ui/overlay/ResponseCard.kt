package com.mewbo.aura.ui.overlay

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.chat.ChatTranscript
import com.mewbo.aura.ui.chat.RunPhase
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.VectorGlyphFill

/**
 * the Streaming-state response card (reference-app-parity pattern), recovered from the
 * pre-v4 `ResponseSheet` (`git show f7e505a:.../ui/overlay/AssistOverlayScreen.kt` - itemsFor/
 * runPhaseFor mapping, same idea, new contract) with measured geometry replacing the old full-bleed
 * 0.75-height sheet: this is a floating CARD (side margins, all-four-corner radius, a gap above the
 * composer pill), not a sheet flush against it. Content is the SAME shared [ChatTranscript] every
 * other surface renders (module CLAUDE.md "never fork chat rendering") - only the container chrome
 * (drag handle, controls row) is new here. Thumbs/share controls are deliberately absent (no
 * backend semantics for either yet - documented deviation, not an oversight).
 */
@Composable
internal fun ResponseCard(
    items: List<ChatItem>,
    runPhase: RunPhase,
    speaking: Boolean,
    onToggleSpeak: () -> Unit,
    onExpand: () -> Unit,
    maxHeight: Dp,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.ResponseCard.sideMargin)
            .heightIn(max = maxHeight)
            .clip(RoundedCornerShape(AuraShape.radiusCard))
            .background(color = AuraColors.surfaceOverlayPill)
            // swallow taps that land on the card's own body so the outside-tap-to-dismiss
            // layer beneath the overlay never fires for a tap ON the response — only genuinely
            // outside-the-content taps dismiss (the SAME "content Surface blocks the scrim" contract
            // Material's own ModalBottomSheet relies on; this card isn't a Surface, so it opts in
            // explicitly). Placed AFTER the side-margin padding, so the card's own margins stay
            // "outside" and still dismiss. detectTapGestures consumes only the DOWN of a tap, so the
            // card's drag handle (swipe-up = expand) and the ChatTranscript's vertical scroll — both
            // movement-based — are untouched.
            .pointerInput(Unit) { detectTapGestures {} },
    ) {
        ResponseCardDragHandle(
            onExpand = onExpand,
            modifier = Modifier
                .align(Alignment.CenterHorizontally)
                .fillMaxWidth(),
        )

        // onRetry/onNotice/onReadAloudToggle/speakingKey: the overlay has no toast host or
        // per-message TTS wiring - it has a conversation-level speaker badge instead (below), and a
        // client-side AssistUiState.Error never reaches this card at all (its own top-level inline
        // ErrorCard handles that, see AssistOverlayScreen) - a documented gap, not an unwired one.
        // sessionEnded = false is correct BY CONSTRUCTION, not an unwired gap: this card only
        // ever renders `beginTurn`'s FIRST turn, which always opens a BRAND-NEW session
        // (`sessionId ?: createSession()`, voice/AssistTurnMachine) - and a fresh session can never
        // be terminated (`terminate_session` stamps `terminated_at` set-once). Every path that
        // could reach an ALREADY-terminated session (continueLastSession/expand/pullUpToApp) hands
        // off to the app's ChatSurface, which owns the terminal state (composer disabled, no Retry).
        ChatTranscript(
            items = items,
            runPhase = runPhase,
            onRetry = {},
            sessionEnded = false,
            speakingKey = null,
            onNotice = {},
            onReadAloudToggle = {},
            // (parity gate): shrink-wrap a short reply instead of always
            // rendering at the card's own heightIn(max) ceiling - see ChatTranscript's own KDoc.
            fillParent = false,
            // the overlay renders a widget's compact SUMMARY card, never the
            // interactive Pyodide WebView (booting a Python kernel in a small floating overlay is
            // wrong). Tapping the summary reuses the SAME expand/handoff the card's own controls use,
            // opening the interactive widget on the app's ChatSurface.
            allowRichWidgets = false,
            onOpenWidgetInApp = onExpand,
            sessionId = null,
            modifier = Modifier.fillMaxWidth(),
        )

        ResponseCardControls(
            speaking = speaking,
            onToggleSpeak = onToggleSpeak,
            onExpand = onExpand,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.Composer.gapTight, vertical = AuraSpacing.Composer.gapTight / 2),
        )
    }
}

/** Centered pill, drag-UP fires [onExpand] - the touch zone is a fixed
 * [RESPONSE_CARD_HANDLE_TOUCH_HEIGHT]-tall strip pinned to the card's own top edge
 * ([Alignment.TopCenter]) rather than [minimumInteractiveComponentSize]'s symmetric expansion,
 * which would bleed the hit area above the card's rounded top corner. */
@Composable
private fun ResponseCardDragHandle(onExpand: () -> Unit, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .height(RESPONSE_CARD_HANDLE_TOUCH_HEIGHT)
            .pointerInput(Unit) {
                // PointerInputScope implements Density - .toPx() is directly callable here without
                // needing an outer LocalDensity capture.
                val thresholdPx = HANDLE_DRAG_EXPAND_THRESHOLD_DP.toPx()
                var accumulated = 0f
                var triggered = false
                detectVerticalDragGestures(
                    onDragStart = { accumulated = 0f; triggered = false },
                    onDragEnd = { accumulated = 0f; triggered = false },
                    onDragCancel = { accumulated = 0f; triggered = false },
                ) { change, dragAmount ->
                    if (triggered) return@detectVerticalDragGestures
                    accumulated += dragAmount
                    if (accumulated <= -thresholdPx) {
                        triggered = true
                        change.consume()
                        onExpand()
                    }
                }
            },
        contentAlignment = Alignment.TopCenter,
    ) {
        Box(
            modifier = Modifier
                .padding(top = AuraSpacing.ResponseCard.dragHandleTopOffset)
                .size(width = AuraSpacing.ResponseCard.dragHandleWidth, height = AuraSpacing.ResponseCard.dragHandleHeight)
                .background(color = AuraColors.textSecondary, shape = AuraShape.radiusPill),
        )
    }
}

/** Read-aloud badge + expand control, right-aligned - deliberately NOT
 * thumbs/share (no backend semantics for either, documented deviation). */
@Composable
private fun ResponseCardControls(
    speaking: Boolean,
    onToggleSpeak: () -> Unit,
    onExpand: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier,
        horizontalArrangement = Arrangement.End,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        OverlayBareIconButton(
            icon = ChatIcons.VolumeUp,
            description = if (speaking) "Stop reading aloud" else "Read aloud",
            tint = if (speaking) AuraColors.accentPrimary else AuraColors.iconPrimary,
            onClick = onToggleSpeak,
            size = AuraSpacing.ResponseCard.speakerBadgeSize,
        )
        OverlayBareIconButton(
            icon = ExpandGlyph,
            description = "Expand",
            tint = AuraColors.iconPrimary,
            onClick = onExpand,
        )
    }
}

/** Shared bare (containerless) icon touch target - the response card's speaker badge/expand
 * control and the post-turn mic re-entry glyph (Rev reference frame 4: "outline glyph post-turn,
 * NOT the filled circle") all reduce to this one shape, DRY per module CLAUDE.md. `internal` rather
 * than `private` because that second caller (`BareMicButton`) lives with the composer bar in
 * `AssistOverlayScreen.kt` - which is exactly the sharing this KDoc already described. */
@Composable
internal fun OverlayBareIconButton(
    icon: ImageVector,
    description: String,
    tint: Color,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    size: Dp = AuraSpacing.Composer.iconSize,
) {
    Box(
        modifier = modifier
            .minimumInteractiveComponentSize()
            .clickable(onClickLabel = description, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(imageVector = icon, contentDescription = description, tint = tint, modifier = Modifier.size(size))
    }
}

/** Hand-rolled "expand"/open-in-full glyph (opposing corner brackets) - `material-icons-extended`
 * isn't in the dependency catalog (see `ui/chat/ChatIcons.kt`'s own header) and this concept has no
 * `material-icons-core` analog; two stroked L-brackets at opposing corners mirrors that file's own
 * StopTile/ContentCopy simplification convention rather than pulling in a new dependency for one
 * glyph. Declared here (not in `ui/chat/ChatIcons.kt`, out of this lane's file ownership) since it's
 * only ever used by [ResponseCardControls]. */
private val ExpandGlyph: ImageVector by lazy {
    ImageVector.Builder(name = "Expand", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
        .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
            moveTo(9f, 3f)
            horizontalLineTo(3f)
            verticalLineTo(9f)
        }
        .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
            moveTo(15f, 21f)
            horizontalLineTo(21f)
            verticalLineTo(15f)
        }
        .build()
}

// no matching AuraSpacing token for either - same convention AuraComposer.kt's own
// AttachmentChipIconSize established for a genuinely missing, non-measured (interaction-design, not
// visual-spec) value. RESPONSE_CARD_HANDLE_TOUCH_HEIGHT is pinned to the card's own top edge
// (Alignment.TopCenter) rather than using minimumInteractiveComponentSize's symmetric expansion,
// which would bleed the hit area above the card's rounded top corner.
private val RESPONSE_CARD_HANDLE_TOUCH_HEIGHT = 32.dp
private val HANDLE_DRAG_EXPAND_THRESHOLD_DP = 24.dp
