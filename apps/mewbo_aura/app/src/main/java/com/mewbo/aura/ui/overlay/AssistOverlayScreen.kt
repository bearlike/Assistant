package com.mewbo.aura.ui.overlay

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.EnterTransition
import androidx.compose.animation.ExitTransition
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.aurora.AuroraEdgeGlow
import com.mewbo.aura.ui.aurora.OverlayScrim
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.chat.RunPhase
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.composer.AuraComposer
import com.mewbo.aura.ui.composer.ComposerOverlayMicCircle
import com.mewbo.aura.ui.composer.ComposerState
import com.mewbo.aura.ui.composer.ComposerStyle
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import com.mewbo.aura.voice.AssistUiState
import kotlin.math.roundToInt
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Callbacks wired by [com.mewbo.aura.voice.AuraSession] into [AssistOverlayScreen] (v4: text-first,
 * handoff-only - see [AssistUiState]'s KDoc; adds the Streaming-state response card's
 * own three: [onStopStreaming] is the composer's stop tile once a turn is generating,
 * [onToggleSpeak] is the card's conversation-level read-aloud badge, [onExpand] is the card's
 * open-in-full control AND the drag handle's swipe-up gesture - both reach the same callback, the
 * host (not this screen) decides what "expand" means). [onStartListening] is the composer's mic tap
 * - `AuraSession` decides there whether `RECORD_AUDIO` is actually grantable before
 * calling into `AssistTurnMachine.startListening`, since only it holds a `Context`; this screen
 * doesn't know or care which way that came out - it's also what re-arms the post-turn bare mic
 * glyph. [onCancelListening] is the LISTENING state's stop-tile tap. [onPullUpToApp] is the
 * composer pill's swipe-up gesture: a soft handoff into the full app
 * carrying the pill's current draft text - the host (`AuraSession` -> `AssistTurnMachine.pullUpToApp`)
 * owns the session-vs-new-chat routing decision; this screen only reports the gesture and the draft
 * it happened over. */
data class AssistOverlayCallbacks(
    val onDismiss: () -> Unit,
    val onSend: (String) -> Unit,
    val onContinueLastSession: () -> Unit,
    val onCancelListening: () -> Unit,
    val onStartListening: () -> Unit,
    val onStopStreaming: () -> Unit,
    val onToggleSpeak: () -> Unit,
    val onExpand: () -> Unit,
    val onPullUpToApp: (draft: String) -> Unit,
)

/**
 * The assist invocation overlay: text-first with a handoff to the app, rendering a floating
 * [ResponseCard] for the Streaming state, capped at ~65% of the overlay's own height (never a
 * full-bleed sheet). Layers
 * bottom to top: [OverlayScrim] (0-180ms fade) -> [AuroraEdgeGlow] (bottom-anchored bloom, ignites
 * 0-450ms at the orb's position) -> swipe-dismiss catcher (scoped above the card when one is
 * showing, so its own scroll doesn't fight a whole-screen drag) -> a bottom-anchored `Column`
 * of [ResponseCard] + the inline error card + the continue-last-session chip + [FloatingComposerBar]
 * -> dismiss chrome. The floating/docking overlay orb was DELETED (user decision): the
 * reference overlay has no orb, and its anchor-chasing machinery was the overlay's top defect
 * source - the brand mark lives in the card's typing indicator and the app surfaces instead.
 *
 * Still no Haze/glass anywhere (spec §2); the card is a plain opaque `surfaceOverlayPill` fill.
 */
@Composable
fun AssistOverlayScreen(
    state: AssistUiState,
    callbacks: AssistOverlayCallbacks,
    modifier: Modifier = Modifier,
) {
    val extras = LocalAssistantExtras.current
    val cardVisible = state is AssistUiState.Streaming
    val edgeGlowState = rememberEdgeGlowState(state, extras.reducedMotion)
    val perimeterBloom = rememberPerimeterBloom(state, extras.reducedMotion)

    var draft by rememberSaveable(stateSaver = TextFieldValue.Saver) { mutableStateOf(TextFieldValue()) }

    // (the mic-invisibility root cause): the composition SURVIVES a session hide->show
    // (AuraSession is reused across invocations), so a leftover draft from a prior visit otherwise
    // sticks around silently - clear it every time the machine settles back to Idle (dismiss/hide).
    LaunchedEffect(state is AssistUiState.Idle) {
        if (state is AssistUiState.Idle) draft = TextFieldValue()
    }

    // The failed text comes back into the draft, editable/resendable through the same composer
    // send affordance any other turn uses (§6.12 quiet error) - keyed on the Error transition so it
    // doesn't fight the user if they're already typing something else.
    LaunchedEffect(state is AssistUiState.Error) {
        val error = state as? AssistUiState.Error ?: return@LaunchedEffect
        draft = TextFieldValue(error.retryText, selection = TextRange(error.retryText.length))
    }

    // The overlay's total height in px, captured once via layout - the swipe-dismiss threshold
    // below needs the FULL screen height, and the card sizing needs it too (max ~65% of screen).
    var fullHeightPx by remember { mutableIntStateOf(0) }

    val density = LocalDensity.current
    val cardMaxHeight = with(density) { (fullHeightPx * AuraSpacing.ResponseCard.maxHeightFraction).toDp() }

    Box(
        modifier = modifier
            .fillMaxSize()
            .onSizeChanged { fullHeightPx = it.height },
    ) {
        AnimatedVisibility(
            visible = state !is AssistUiState.Idle,
            enter = fadeIn(if (extras.reducedMotion) snap() else tween(AuraMotion.scrimFadeMs)),
            exit = fadeOut(
                if (extras.reducedMotion) {
                    snap()
                } else {
                    tween((AuraMotion.scrimFadeMs / AuraMotion.dismissSpeedMultiplier).toInt())
                },
            ),
        ) {
            OverlayScrim()
        }

        // Outside-tap-to-dismiss, the GAP layer. Compose hit-testing routes a pointer event
        // only to the TOPMOST hit sibling chain at that position — a lower full-screen sibling never
        // receives the event wherever an overlapping pointer-input sibling sits above it, and
        // consumption is irrelevant (the lower node isn't in the hit path AT ALL). So this
        // catch-all fires ONLY in regions no other pointer layer covers: the card's side margins,
        // and (while a card shows) the band BELOW the scoped swipe catcher and ABOVE the card. In
        // the no-card states this whole layer is shadowed by the then-full-screen swipe catcher —
        // which is exactly why that catcher ALSO routes taps to onDismiss (below). A single
        // under-everything layer is provably insufficient: device-verified, a tap in the
        // swipe catcher's region died in its drag detector and never reached here. The composer
        // pill (a Material `Surface`), the companion chips (`clickable`), the response card
        // (tap-absorbed at its own container, below) and the ✕ each win their own positions and
        // swallow their taps, so a tap only dismisses on genuinely-empty scrim. Routes into the
        // SAME onDismiss the ✕ uses, so draft-clear / TTS-stop / haptics / state cleanup AND the
        // state->Idle smooth exit choreography (glow extinguishing WITH the pill over
        // OVERLAY_GLOW_DISMISS_MS) all ride the ONE teardown path (AuraSession.dismissSession ->
        // machine.dismiss() + hide()). Reduced motion needs nothing extra: that exit already snaps
        // under `extras.reducedMotion`.
        Box(
            modifier = Modifier
                .fillMaxSize()
                // a11y: THE single TalkBack-equivalent of the outside tap, and it lives here (not on
                // the swipe catcher) on purpose — an accessibility action is dispatched via the
                // SEMANTICS tree, not pointer hit-testing, so TalkBack reaches this node's onClick
                // even though touch events in the swipe catcher's region never do (the sighted tap
                // there is handled on the catcher itself). The swipe catcher carries no semantics,
                // so it isn't a competing a11y node. Label-only, NO contentDescription, so this
                // full-screen node contributes a dismiss ACTION without announcing itself as a giant
                // labelled region that would shadow the composer/card in the a11y tree or steal
                // focus from the composer (the ✕ keeps its own independent "Dismiss" label).
                .semantics { onClick(label = "Dismiss assistant") { callbacks.onDismiss(); true } }
                .pointerInput(Unit) { detectTapGestures { callbacks.onDismiss() } },
        )

        AuroraEdgeGlow(
            state = edgeGlowState,
            colors = AuraColors.auroraOverlayLiveBloom,
            perimeterBloom = perimeterBloom,
            // The overlay's full multi-hue aurora + persistent edge-lit perimeter.
            hueDriftAmount = OVERLAY_AURORA_HUE_DRIFT,
            perimeterPresence = OVERLAY_PERIMETER_PRESENCE,
            // Shares the pill's own exit window (below) so the glow extinguishes WITH the pill,
            // never as a ghost after it or a pop before it.
            dismissFadeMs = OVERLAY_GLOW_DISMISS_MS,
        )

        // Outside-content dismiss catcher — handles BOTH a plain TAP and a deliberate downward
        // SWIPE, both -> onDismiss. The tap is caught HERE, not delegated to the gap layer above in
        // code (below in z): Compose hit-testing routes a pointer event only to the TOPMOST hit
        // sibling chain, so across this catcher's region the event reaches ONLY this Box — the gap
        // layer never sees it (device-verified: without its own tap detector, a tap here dies
        // silently in the drag detector, which only fires past slop). Two pointerInputs on one node
        // compose fine — the tap detector fires on a stationary press, the drag detector on
        // movement past slop. Still scoped ABOVE the card's
        // reserved region while one shows (`1f - maxHeightFraction`): a whole-screen catcher
        // competes with the card's own ChatTranscript SCROLL (Critical — the DRAG needs
        // this scoping; don't reintroduce a whole-screen drag catcher). When no card shows it is
        // full-screen, so its tap handler is what dismisses every empty-region tap in the Ready/
        // Listening/Sending/Error states (the pill/chips/✕ draw ABOVE it and win their own taps).
        // The SWIPE still requires accumulated distance (not a per-event delta) covering >=15% of
        // the overlay's height.
        Box(
            modifier = Modifier
                .align(Alignment.TopCenter)
                .fillMaxWidth()
                .fillMaxHeight(fraction = if (cardVisible) 1f - AuraSpacing.ResponseCard.maxHeightFraction else 1f)
                // Plain tap in this region -> dismiss. Separate pointerInput from the drag below so
                // the two gesture detectors keep independent scopes (a tap = no slop, so the drag
                // detector abandons it; a swipe moves past slop, so the tap detector abandons it).
                .pointerInput(Unit) { detectTapGestures { callbacks.onDismiss() } }
                .pointerInput(fullHeightPx) {
                    if (fullHeightPx <= 0) return@pointerInput
                    val threshold = fullHeightPx * SWIPE_DISMISS_HEIGHT_FRACTION
                    var accumulated = 0f
                    var triggered = false
                    detectVerticalDragGestures(
                        onDragStart = { accumulated = 0f; triggered = false },
                        onDragEnd = { accumulated = 0f; triggered = false },
                        onDragCancel = { accumulated = 0f; triggered = false },
                    ) { change, dragAmount ->
                        if (triggered) return@detectVerticalDragGestures
                        accumulated += dragAmount
                        if (accumulated >= threshold) {
                            triggered = true
                            change.consume()
                            callbacks.onDismiss()
                        }
                    }
                },
        )

        Column(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .fillMaxWidth()
                .systemBarsPadding(),
            // NO imePadding() here, deliberately (keyboard-gap fix, measured on-device): the
            // WindowManager force-pans TYPE_VOICE_INTERACTION windows for the IME regardless of
            // app-side softInputMode (ADJUST_NOTHING was set in onCreate AND re-asserted via
            // attributes in onShow - dumpsys kept reporting adjust=pan and the surface visibly
            // panned). Pan alone parks the focused field exactly above the keyboard; adding
            // imePadding on top double-shifted the composer ~900px above it. Pan is the single
            // IME mechanism for this window - never add imePadding to this tree (the in-app chat
            // is an ACTIVITY window where the opposite rule holds - ChatSurface needs imePadding).
            // Bottom-packed because the card's weight (below) expands this Column to the full
            // available height - without an explicit Arrangement the cluster would float to the top.
            verticalArrangement = Arrangement.Bottom,
        ) {
            // weight(1f, fill = false): the card is measured LAST with exactly the space left over
            // after the pill/error/chip claim theirs - so when the IME opens (imePadding above
            // shrinks this Column's inner max), the CARD is what yields, shrinking its cap to the
            // remaining space while the composer stays pinned above the keyboard; fill=false keeps
            // short replies shrink-wrapped. The fixed 65%-of-screen cap alone (heightIn inside
            // ResponseCard) was measured against the FULL window height, which never changes with
            // the IME (the session window's frame is static - voice/CLAUDE.md) - stacking overflow
            // instead of yielding. Same measurement-order law as the composer-options sheet's
            // squeeze fix: weighted content yields, fixed chrome never gets coerced.
            AnimatedVisibility(
                visible = cardVisible,
                modifier = Modifier.weight(1f, fill = false),
                enter = fadeIn() + expandVertically(expandFrom = Alignment.Bottom),
                exit = fadeOut() + shrinkVertically(shrinkTowards = Alignment.Bottom),
            ) {
                ResponseCard(
                    items = itemsFor(state),
                    runPhase = runPhaseFor(state),
                    speaking = (state as? AssistUiState.Streaming)?.speaking ?: false,
                    onToggleSpeak = callbacks.onToggleSpeak,
                    onExpand = callbacks.onExpand,
                    maxHeight = cardMaxHeight,
                    modifier = Modifier.padding(bottom = AuraSpacing.ResponseCard.gapToPill),
                )
            }

            CompanionChipSlot(state = state, callbacks = callbacks, reducedMotion = extras.reducedMotion)

            FloatingComposerBar(
                state = state,
                draft = draft,
                onDraftChange = { draft = it },
                callbacks = callbacks,
                reducedMotion = extras.reducedMotion,
            )
        }

        OverlayChrome(state = state, onDismiss = callbacks.onDismiss)
    }
}

/** The ONE companion-affordance slot above the pill - contextual chips float
 * here (today: the inline error card and the continue-last-session chip; nothing else - YAGNI).
 * Shared fade+rise entrance so slot content arrives as part of the overlay's choreography
 * instead of popping. */
@Composable
private fun CompanionChipSlot(
    state: AssistUiState,
    callbacks: AssistOverlayCallbacks,
    reducedMotion: Boolean,
) {
    // ONE shared enter/exit spec pair for both chips (de-duplicates the two identical inline specs).
    // Under reduced motion the slot snaps in and out with NO choreography - instant, exactly like
    // pre-branch (the chips were plain inline `if` blocks then) and matching how the scrim/composer
    // entrance in this same file drop to a snap when extras.reducedMotion is set.
    val chipEnter: EnterTransition = if (reducedMotion) {
        EnterTransition.None
    } else {
        fadeIn(AuraMotion.composerMorphSpring) + slideInVertically { it / 2 }
    }
    val chipExit: ExitTransition = if (reducedMotion) ExitTransition.None else fadeOut(AuraMotion.composerMorphSpring)

    // AnimatedVisibility keeps rendering its content off a SNAPSHOT throughout the EXIT fade, but the
    // outer `state` has already moved on by then - re-deriving the typed payload from the LIVE state
    // renders blank mid-exit (Error's `state as? Error ?: return` fades nothing; the continue chip's
    // `lastSessionTitle.orEmpty()` fades an empty title). Latch the last-seen payload WHILE the state
    // is that kind and render the latched copy, so the exit animates the content the user actually
    // saw. The latch is nullable, so the content guard stays crash-safe (no force-cast).
    var lastError by remember { mutableStateOf<AssistUiState.Error?>(null) }
    (state as? AssistUiState.Error)?.let { lastError = it }
    AnimatedVisibility(visible = state is AssistUiState.Error, enter = chipEnter, exit = chipExit) {
        val error = lastError ?: return@AnimatedVisibility
        ErrorCard(
            reason = error.reason,
            // A blank retryText means there was never a queued send to retry - the mic-
            // permission notice (AssistTurnMachine.microphonePermissionUnavailable)
            // is the one Error today with nothing resendable.
            onRetry = { callbacks.onSend(error.retryText) }.takeIf { error.retryText.isNotBlank() },
            modifier = Modifier.padding(
                start = AuraSpacing.Composer.horizontalMargin,
                end = AuraSpacing.Composer.horizontalMargin,
                bottom = AuraSpacing.Composer.gapTight,
            ),
        )
    }

    var lastSessionTitle by remember { mutableStateOf<String?>(null) }
    (state as? AssistUiState.Ready)?.lastSessionTitle?.let { lastSessionTitle = it }
    AnimatedVisibility(
        visible = state is AssistUiState.Ready && state.lastSessionTitle != null,
        enter = chipEnter,
        exit = chipExit,
    ) {
        val title = lastSessionTitle ?: return@AnimatedVisibility
        ContinueLastSessionChip(
            title = title,
            onClick = callbacks.onContinueLastSession,
            modifier = Modifier.padding(
                start = AuraSpacing.Composer.horizontalMargin,
                bottom = AuraSpacing.Composer.gapTight,
            ),
        )
    }
}

/** Items the response card should show - only [AssistUiState.Streaming] carries a
 * payload under the new contract (Sending/Error no longer do; a client-side [AssistUiState.Error]
 * renders via the separate inline [ErrorCard], not through the card/[ChatTranscript] path at all). */
private fun itemsFor(state: AssistUiState): List<ChatItem> = when (state) {
    is AssistUiState.Streaming -> state.items
    else -> emptyList()
}

private fun runPhaseFor(state: AssistUiState): RunPhase = when (state) {
    is AssistUiState.Streaming -> if (state.done) RunPhase.Done else RunPhase.Streaming
    else -> RunPhase.Idle
}

/** The post-turn voice-follow-up affordance - a bare glyph, not
 * [ComposerOverlayMicCircle]'s filled circle (reference frame 4 shows the outline glyph here, the
 * filled circle is READY's own trailing slot only). Wired to the same [AssistOverlayCallbacks.onStartListening]
 * READY's mic circle uses - both mean "begin a new listening turn," just from different resting
 * states. */
@Composable
private fun BareMicButton(onClick: () -> Unit, modifier: Modifier = Modifier) {
    OverlayBareIconButton(
        icon = ChatIcons.Mic,
        description = "Voice input",
        tint = AuraColors.iconPrimary,
        onClick = onClick,
        modifier = modifier,
    )
}

@Composable
private fun FloatingComposerBar(
    state: AssistUiState,
    draft: TextFieldValue,
    onDraftChange: (TextFieldValue) -> Unit,
    callbacks: AssistOverlayCallbacks,
    reducedMotion: Boolean,
    modifier: Modifier = Modifier,
) {
    val listening = state as? AssistUiState.Listening
    val streaming = state as? AssistUiState.Streaming
    // the stop tile only applies while a turn is actively generating - once
    // Streaming.done, isStreaming flips false and the composer re-arms (Typing/Idle per the draft),
    // same contract ComposerState.resolve already expects.
    val isStreaming = streaming != null && !streaming.done
    val composerState = ComposerState.resolve(
        draftText = draft.text,
        isDictating = listening != null,
        isStreaming = isStreaming,
        rmsDb = listening?.rmsDb ?: 0f,
        // AssistUiState.Listening.partial defaults to "" (non-null) the instant dictation starts,
        // not null - AuraComposer's DictationCenterContent branches on partialText != null to pick
        // RMS bars vs. the finalizing-transcript text, so passing the raw "" made that branch
        // permanently true and the bars never rendered. takeIf restores the null-means-"nothing
        // transcribed yet" contract DictationCenterContent expects.
        partialText = listening?.partial?.takeIf { it.isNotBlank() },
    )
    val onSendClick = {
        val text = draft.text
        if (text.isNotBlank()) {
            onDraftChange(TextFieldValue())
            callbacks.onSend(text)
        }
    }

    // §7.0: "slide up 24dp + fade in, starting 120ms after onShow, settled by 340ms" - entrance is
    // keyed off the SAME `state !is Idle` signal the scrim's own AnimatedVisibility already uses,
    // just via a manual Animatable pair (not AnimatedVisibility) so the entrance can carry a genuine
    // start DELAY, which AnimatedVisibility has no parameter for.
    val visible = state !is AssistUiState.Idle
    val entranceOffsetDp = remember { Animatable(if (reducedMotion) 0f else COMPOSER_ENTRANCE_SLIDE_DP) }
    val entranceAlpha = remember { Animatable(if (reducedMotion) 1f else 0f) }
    LaunchedEffect(visible, reducedMotion) {
        if (reducedMotion) {
            // M8: "appear without slide" - fade only, no travel, no start delay.
            entranceOffsetDp.snapTo(0f)
            entranceAlpha.snapTo(1f)
        } else if (visible) {
            delay(AuraMotion.barSlideStartMs.toLong())
            launch { entranceOffsetDp.animateTo(0f, AuraMotion.invocationSpring) }
            launch { entranceAlpha.animateTo(1f, AuraMotion.invocationSpring) }
        } else {
            // Dismiss: reverse at dismissSpeedMultiplier - a spring has no "duration" to divide, so
            // the exit uses a tween at the scaled window instead, mirroring the scrim's own exit.
            // Same OVERLAY_GLOW_DISMISS_MS the glow's own dismissFadeMs reads (above) - one
            // constant, not two parallel formulas, so the glow can't drift out of sync with the
            // pill it's meant to extinguish alongside.
            launch { entranceOffsetDp.animateTo(COMPOSER_ENTRANCE_SLIDE_DP, tween(OVERLAY_GLOW_DISMISS_MS)) }
            launch { entranceAlpha.animateTo(0f, tween(OVERLAY_GLOW_DISMISS_MS)) }
        }
    }

    // Pull-up (swipe-up) on the composer pill softly hands the overlay into the full app (user
    // directive). The pill drag-FOLLOWS the finger upward; releasing past
    // PILL_PULL_UP_THRESHOLD_DP commits (the machine fires the standard `settle` release haptic and
    // routes into the app - session-vs-new-chat decided host-side), below it springs back via the
    // shared AuraMotion.composerMorphSpring. The offset MUST reset on Idle: this composition survives
    // a session hide->show (ui/overlay/CLAUDE.md's per-invocation reset law - the exact reason the
    // `draft` above is Idle-cleared too), so a committed/partial pull left non-zero would otherwise
    // resurrect as a shifted-up pill on the NEXT invocation. detectVerticalDragGestures claims the
    // gesture only after vertical touch-slop, so the pill's tap/mic/text interactions are untouched.
    val pullScope = rememberCoroutineScope()
    val pullOffsetPx = remember { Animatable(0f) }
    // rememberUpdatedState so the (Unit-keyed, never-restarted) gesture reads the CURRENT draft/
    // callback at release, not the stale ones captured when the pointerInput first ran.
    val currentDraftText by rememberUpdatedState(draft.text)
    val currentOnPullUp by rememberUpdatedState(callbacks.onPullUpToApp)
    LaunchedEffect(state is AssistUiState.Idle) {
        if (state is AssistUiState.Idle) pullOffsetPx.snapTo(0f)
    }

    // v4 hides the C1 voice-MODE square tile only (that's the reference app's live-voice-mode analog, a
    // distinct product surface, non-goal here) - AuraComposer's default trailing cluster couples
    // that tile to the bare mic glyph as one pair, so this override still replaces the WHOLE
    // cluster for ComposerState.Idle with one of two things (the overlay orb was DELETED - user
    // decision on v5's trail: the reference overlay has no floating orb, and the orb's
    // anchor-chasing machinery was the overlay's top defect source): the post-turn bare mic glyph
    // once Streaming.done, or the filled mic circle otherwise (disabled during the brief Sending
    // window - the whole composer is disabled then anyway). A non-blank draft (Typing) or an actual
    // Dictation falls through to AuraComposer's own default cluster untouched - that's where the C5
    // send-arrow morph and the voice-path's stop tile still live, outside this file's ownership.
    val trailingAccessory: (@Composable () -> Unit)? = if (composerState is ComposerState.Idle) {
        {
            if (streaming?.done == true) {
                BareMicButton(onClick = callbacks.onStartListening)
            } else {
                ComposerOverlayMicCircle(
                    enabled = state !is AssistUiState.Sending,
                    onClick = callbacks.onStartListening,
                    size = AuraSpacing.Composer.overlayActionCircleSize,
                )
            }
        }
    } else {
        null
    }

    AuraComposer(
        state = composerState,
        draft = draft,
        onDraftChange = onDraftChange,
        onSend = onSendClick,
        // reachable now - ComposerState.Streaming(hasDraft = false)'s default trailing
        // cluster renders the Stop glyph via this exact callback whenever the user hasn't typed
        // over the in-flight turn.
        onStop = callbacks.onStopStreaming,
        onMicTap = NO_OP, // unreachable: Idle always supplies a trailingAccessory (mic circle/glyph) above.
        onDictationStop = callbacks.onCancelListening, // LISTENING's stop tile - reachable via the real mic tap now too.
        onVoiceModeTap = NO_OP, // no separate voice-mode entry in v4 - the square tile itself stays hidden (see above).
        style = ComposerStyle.FloatingOverlay,
        enabled = state !is AssistUiState.Sending,
        trailingAccessory = trailingAccessory,
        showDragHandle = true,
        modifier = modifier
            .fillMaxWidth()
            .offset(y = entranceOffsetDp.value.dp)
            // Live pull-up follow, ON TOP of the entrance slide - a second offset so the two never
            // fight over one value.
            .offset { IntOffset(x = 0, y = pullOffsetPx.value.roundToInt()) }
            .graphicsLayer { alpha = entranceAlpha.value }
            // overlay-only pill parity margin (38dp measured) - distinct from the
            // docked in-app composer's own AuraSpacing.Composer.horizontalMargin (16dp, untouched).
            .padding(horizontal = AuraSpacing.Composer.overlayHorizontalMargin, vertical = AuraSpacing.Composer.bottomInset)
            // pointerInput placed AFTER the horizontal margin padding so the pull hit-area is the
            // pill itself, not the 38dp side gutters (keeps the swipe-dismiss catcher / edge gestures
            // clear). Same accumulate-then-threshold shape as ResponseCardDragHandle above.
            .pointerInput(Unit) {
                // PointerInputScope is a Density - .toPx() is directly callable.
                val commitThresholdPx = PILL_PULL_UP_THRESHOLD_DP.toPx()
                detectVerticalDragGestures(
                    onDragEnd = {
                        if (pullOffsetPx.value <= -commitThresholdPx) {
                            currentOnPullUp(currentDraftText)
                        } else {
                            pullScope.launch { pullOffsetPx.animateTo(0f, AuraMotion.composerMorphSpring) }
                        }
                    },
                    onDragCancel = {
                        pullScope.launch { pullOffsetPx.animateTo(0f, AuraMotion.composerMorphSpring) }
                    },
                ) { change, dragAmount ->
                    change.consume()
                    // Follow upward pulls only (coerce <= 0) - a downward drag never pushes the pill
                    // below its resting position.
                    pullScope.launch { pullOffsetPx.snapTo((pullOffsetPx.value + dragAmount).coerceAtMost(0f)) }
                }
            },
    )
}

/** Dismiss button - everything that isn't the card/composer/orb/error card/chip. There is
 * deliberately no mid-screen listening caption: the composer pill's own `DictationCenterContent`
 * (via `ComposerState.Dictation.partialText`) is the sole live-transcription surface, since a
 * second, competing caption would be a redundant surface, not a feature. There is likewise no
 * separate idle hint - the composer's own "Ask Mewbo" placeholder already covers it, and true
 * [AssistUiState.Idle] is never actually on screen - see its own KDoc. */
/** Non-visual state channel: a zero-sized polite live region — TalkBack announces each
 * state-KIND change (rms/delta ticks produce the identical string, so no re-announce spam).
 * Dismissal is deliberately unannounced: the window teardown races TalkBack; focus returning to
 * the host app is the dismissal signal. */
@Composable
private fun OverlayStateAnnouncer(state: AssistUiState, modifier: Modifier = Modifier) {
    val announcement = when (state) {
        AssistUiState.Idle -> null
        is AssistUiState.Ready -> "Mewbo Assistant ready"
        is AssistUiState.Listening -> "Mewbo Assistant, listening"
        AssistUiState.Sending -> "Processing"
        is AssistUiState.Streaming -> if (state.done) "Response ready" else "Responding"
        is AssistUiState.Error -> "Error: ${state.reason}"
    } ?: return
    Box(
        modifier = modifier
            .size(ANNOUNCER_NODE_SIZE_DP)
            .semantics {
                liveRegion = LiveRegionMode.Polite
                contentDescription = announcement
            },
    )
}

@Composable
private fun OverlayChrome(state: AssistUiState, onDismiss: () -> Unit) {
    Box(modifier = Modifier.fillMaxSize().systemBarsPadding().padding(OVERLAY_EDGE_PADDING)) {
        OverlayStateAnnouncer(state = state)
        IconButton(
            onClick = onDismiss,
            modifier = Modifier
                .align(Alignment.TopStart)
                // Without this the icon inherits LocalContentColor (Color.Black under
                // MaterialTheme) over the dark scrim, rendering invisible. Same surfaceIconScrim
                // disc the chat top bar uses for icons over content.
                .background(color = AuraColors.surfaceIconScrim, shape = AuraShape.radiusPill),
        ) {
            Icon(imageVector = Icons.Filled.Close, contentDescription = "Dismiss", tint = AuraColors.iconPrimary)
        }
    }
}

@Composable
private fun ContinueLastSessionChip(
    title: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    // §6.10: "same token family as the C5 pill" - surfaceOverlayPill, not a FilterChip's default M3 look.
    Box(
        modifier = modifier
            .background(color = AuraColors.surfaceOverlayPill, shape = AuraShape.radiusPill)
            .clickable(onClickLabel = "Continue $title", onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 10.dp),
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(
                imageVector = Icons.AutoMirrored.Filled.List,
                contentDescription = null,
                tint = AuraColors.iconPrimary,
                modifier = Modifier.size(18.dp),
            )
            Text(text = "Continue “$title”", style = AuraType.chipLabel, color = AuraColors.textPrimary, maxLines = 1)
        }
    }
}

private val NO_OP: () -> Unit = {}

private const val SWIPE_DISMISS_HEIGHT_FRACTION = 0.15f
private val OVERLAY_EDGE_PADDING = 16.dp

// 1dp = the smallest non-prunable semantics node for OverlayStateAnnouncer's polite live region -
// a genuinely zero-sized node risks layout-pruning, dropping the TalkBack channel. Deliberately
// NOT an AuraSpacing token: an accessibility implementation detail, not a design/visual value.
private val ANNOUNCER_NODE_SIZE_DP = 1.dp
private val COMPOSER_ENTRANCE_SLIDE_DP = 24f // §7.0: "slide up 24dp"

// Pull-up commit distance for the composer pill. Deliberately larger
// than the response card's 24dp handle threshold: the WHOLE pill translates here (not a dedicated
// 32dp handle strip), so a more committed pull avoids accidental handoffs while scrolling/typing.
// No AuraSpacing token yet - flagged for later tokenization (AuraSpacing additions are out of this
// task's lane), same local-constant convention the two thresholds above already use.
private val PILL_PULL_UP_THRESHOLD_DP = 48.dp

// [R4] Issue-D exit choreography: the glow's dismiss fade shares the pill's own exit
// window ((settle − start) / dismissSpeedMultiplier ≈ 169ms) so glow and pill extinguish
// TOGETHER — the glow neither outlives the pill (ghost light) nor vanishes first (pop). Uses
// AuroraEdgeGlow's hold-frame fade (never a uniform snap - §7.15). FloatingComposerBar's own exit
// tween reads this SAME constant (not a parallel formula) so the two can't drift apart.
// `internal` rather than private: OverlayAnimations.kt's rememberPerimeterBloom delays its Idle
// reset by this same window, which is the whole point of there being ONE constant.
internal val OVERLAY_GLOW_DISMISS_MS =
    ((AuraMotion.barSlideSettleMs - AuraMotion.barSlideStartMs) / AuraMotion.dismissSpeedMultiplier).toInt()

// [R5] The overlay is the ONLY surface that lights up the full multi-hue aurora: it
// drives the hue-drift field (A blue -> B violet -> C ember) and the persistent edge-lit perimeter
// floor at full strength. Chat keeps both at 0 (AuroraEdgeGlow's defaults → the legacy bottom-only,
// single-hue byte-path). Caller-side design knobs, not measured tokens (ui/aurora/CLAUDE.md
// provenance rule) — device feedback on 0.0.30-debug: "make it a pretty multi-hue aurora, stronger
// and fluid towards the edges."
private const val OVERLAY_AURORA_HUE_DRIFT = 1f
private const val OVERLAY_PERIMETER_PRESENCE = 1f
