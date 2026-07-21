package com.mewbo.aura.ui.chat

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.Icon
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.aurora.AuroraEdgeGlow
import com.mewbo.aura.ui.aurora.EdgeGlowState
import com.mewbo.aura.ui.common.ChatOverflowMenu
import com.mewbo.aura.ui.common.OverflowMenuItem
import com.mewbo.aura.ui.composer.ComposerOptionsSheet
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import kotlinx.coroutines.delay

/**
 * Top-bar chrome around [ChatSurface] (apps/mewbo_aura/CLAUDE.md "one chat tree, two hosts" - this
 * is the activity/nav host; the assist overlay is the other). Chat is now the app's single
 * root/only destination reached via the drawer (`onMenuTap`) and "New chat" (`onNewChat`) rather
 * than a back-stack push - there is no `onBack` (spec: no standalone landing/sessions screen).
 */
@Composable
fun ChatScreen(
    sessionId: String?,
    /** the assist-overlay handoff's raw `InputModality.name` string
     * (`AuraNavHost`'s nav-route arg); `null` for every ordinary (non-handoff) navigation. Passed
     * straight through to [ChatViewModel.bind] as a plain `String?` - this `ui/` layer never parses
     * it into `com.mewbo.aura.voice.InputModality` itself (package layering: only the view model is
     * a legal `voice/` consumer). */
    handoffModality: String? = null,
    onMenuTap: () -> Unit,
    onNewChat: () -> Unit,
    onNotice: (String) -> Unit,
    modifier: Modifier = Modifier,
    /** Navigate to another session — `AuraNavHost` passes the SAME lambda the drawer's own
     * open-a-session rows use (one `navigate` block, not a second copy of it). Only the two fork
     * actions in [MessageActionsSheet] call it: a fork lands in a BRAND-NEW session and the user is
     * taken there. Defaults to a no-op so `ChatPreviewActivity`-style hosts keep compiling. */
    onOpenSession: (String) -> Unit = {},
    viewModel: ChatViewModel = hiltViewModel(),
) {
    // The long-pressed user message whose actions sheet is open — absence IS dismissal, the same
    // `actionTarget: T?`-as-visibility shape `AuraDrawerContent` uses for `SessionActionsSheet`.
    var messageActionTarget by remember { mutableStateOf<ChatItem.UserBubble?>(null) }
    LaunchedEffect(sessionId) {
        viewModel.bind(sessionId, handoffModality)
        // The sheet's target belongs to the session being LEFT. This composable's `remember`ed state
        // SURVIVES a session switch (the drawer navigates with `launchSingleTop` + `popUpTo` onto the
        // same `chat` route, reusing this very back-stack entry - the same fact SessionBinding exists
        // for), so without this an open sheet would keep hovering over the NEW session, still holding
        // the old one's bubble and still able to fire actions anchored in it.
        messageActionTarget = null
    }
    val state by viewModel.state.collectAsStateWithLifecycle()
    val clipboard = LocalClipboardManager.current
    var modelPickerOpen by remember { mutableStateOf(false) }
    var composerOptionsOpen by remember { mutableStateOf(false) }

    LaunchedEffect(modelPickerOpen) {
        if (modelPickerOpen) viewModel.loadModelsIfNeeded(onNotice)
    }
    LaunchedEffect(composerOptionsOpen) {
        if (composerOptionsOpen) {
            // Models are also needed here (not just the model picker) to gate the Photos pill on
            // vision support (ComposerScope.kt's caller, ChatUiState.selectedModel).
            viewModel.loadModelsIfNeeded(onNotice)
            viewModel.refreshComposerScope(onNotice)
        }
    }

    val photosLauncher = rememberLauncherForActivityResult(ActivityResultContracts.PickMultipleVisualMedia()) { uris ->
        if (uris.isNotEmpty()) viewModel.stageAttachments(uris, onNotice)
    }
    val filesLauncher = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) viewModel.stageAttachments(uris, onNotice)
    }

    // the OS runtime grant is the SOLE dictation gate - no in-app consent layer
    // (standing rule). Denial (first-ask or permanent) just leaves the mic tappable again with
    // nothing started - the system dialog itself handles rationale/"don't ask again".
    val context = LocalContext.current
    val recordAudioLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) viewModel.startDictation()
    }

    // Same derivation ChatSurface uses for the wash itself - kept independent rather than threaded
    // as a shared flag (both read the one source of truth, `state.runPhase`).
    val overWash = state.runPhase == RunPhase.Sending || state.runPhase == RunPhase.Streaming

    // (user directive, two rounds): the landing/chat liveness glow is the BOTTOM
    // bloom (the overlay's own AuroraEdgeGlow, reused - one shader family, no wash fork), the top
    // stays clean, and it renders HERE behind a TRANSPARENT Scaffold so the canvas + glow run
    // truly edge-to-edge under the header, status bar, and navigation bar (frameless, reference
    // parity). Inside the Scaffold body it stopped at the topBar slot's edge and the header sat on
    // a solid containerColor band. Same greeting/runPhase source of truth ChatSurface reads;
    // duplicated per the overWash precedent above rather than threaded.
    // User directive 2026-07-04: the resting state is a SOLID background - the ambient bottom glow is
    // NOT permanent. It appears briefly on a fresh invocation (this screen freshly opened) AND is
    // re-armed each time a run COMPLETES for AMBIENT_INVOCATION_WINDOW_MS, then fades to
    // Hidden; the Thinking glow owns the Sending/Streaming phase and always wins over either linger.
    // Cheap + testable: delayed flag flips, and the pure `chatGlowState` decision below. reducedMotion
    // needs no branch here - AuroraEdgeGlow already snaps its visibility (no fade) and renders a
    // static frame, so this reads as "static frame then hide" for it, and a smooth fade otherwise.
    // Fresh-invocation ambient breathe (existing): active for the linger window on a truly-fresh
    // (empty) chat screen, then fades to solid rest.
    var freshInvocationActive by remember { mutableStateOf(true) }
    LaunchedEffect(Unit) {
        delay(AMBIENT_INVOCATION_WINDOW_MS)
        freshInvocationActive = false
    }

    // Completion linger: re-fire the SAME bounded ambient breathe every time a run
    // COMPLETES, so a landed response gets the "just surfaced" aurora the fresh screen does, then
    // eases back to solid rest — the run-end glow now blooms from Thinking's contracted hug into the
    // wider Listening pool and lingers, instead of easing straight to black. DESIGN.md §5's bounded,
    // non-permanent-aura law holds: this RE-ARMS the linger, it does not make it persistent (a new
    // send cancels it via the isRunInFlight branch below, and it self-clears after the window). A
    // monotonic arm token captures each transition INTO RunPhase.Done; the timer effect keyed on it
    // restarts the window cleanly on a re-arm and always runs to its own reset even if runPhase later
    // settles to Idle mid-linger (a plain LaunchedEffect(runPhase) timer would be cancelled by that
    // Done→Idle change and strand the flag set). Unlike the fresh-invocation breathe it does NOT
    // require an empty transcript — that is the whole point: it fires because a response just landed
    // into a non-empty transcript.
    var completionLingerActive by remember { mutableStateOf(false) }
    var completionArmToken by remember { mutableStateOf(0) }
    LaunchedEffect(state.runPhase) {
        if (state.runPhase == RunPhase.Done) completionArmToken++
    }
    LaunchedEffect(completionArmToken) {
        if (completionArmToken == 0) return@LaunchedEffect // no run has completed yet
        completionLingerActive = true
        delay(AMBIENT_INVOCATION_WINDOW_MS)
        completionLingerActive = false
    }

    val glowState = chatGlowState(
        runPhase = state.runPhase,
        freshInvocationActive = freshInvocationActive,
        completionLingerActive = completionLingerActive,
        transcriptEmpty = state.items.isEmpty(),
        loadingHistory = state.isLoadingHistory,
    )
    // User directive 2026-07-04: the ACTIVE (processing/invocation) glow should read slightly
    // livelier - "speed of the Aura slightly visibly higher" - while an idle chat stays exactly as
    // tuned. Smallest mechanism: bump ONLY the wave-drift speed for the Thinking state; the ambient
    // Listening/Hidden states keep the default 1f, and the user-tuned reach/center-weight/alpha are
    // untouched.
    val glowSpeedScale = if (glowState == EdgeGlowState.Thinking) IN_APP_GLOW_ACTIVE_SPEED_SCALE else 1f

    Box(modifier = modifier.fillMaxSize().background(AuraColors.surfaceCanvas)) {
        AuroraEdgeGlow(
            state = glowState,
            reachScale = IN_APP_GLOW_REACH_SCALE,
            centerWeightOverride = IN_APP_GLOW_CENTER_WEIGHT,
            alphaScale = IN_APP_GLOW_ALPHA_SCALE,
            speedScale = glowSpeedScale,
            // User directive: the bottom glow must ease OFF over >=3s when a run ends, never snap to
            // black (an abrupt on->off flash is a photosensitivity trigger). Caller-side knob so the
            // assist overlay keeps its own quick dismiss (it passes no override).
            dismissFadeMs = AuraMotion.edgeRestFadeMs,
            modifier = Modifier.fillMaxSize(),
        )

        Scaffold(
            containerColor = Color.Transparent,
        topBar = {
            ChatTopBar(
                sessionOpen = sessionId != null,
                overWash = overWash,
                modelDisplayName = state.models?.effectiveShortName(state.selectedModel) ?: "Core",
                pickerExpanded = modelPickerOpen,
                onMenuTap = onMenuTap,
                onNewChat = onNewChat,
                onModelTap = { modelPickerOpen = true },
                onCopyConversation = {
                    clipboard.setText(AnnotatedString(formatTranscriptForCopy(state.items)))
                    onNotice("Copied")
                },
            )
        },
    ) { padding ->
        // remember, not a fresh ChatCallbacks(...) every recomposition: `state` re-emits on every
        // delta/chip/todos event, and ChatScreen's body re-runs each time - an inline construction
        // here would hand ChatSurface's whole subtree a brand-new (non-referentially-equal)
        // `onReadAloudToggle`/`onNotice` on every one of those emissions, which breaks downstream
        // rows' parameter stability and forces them to recompose for updates that have nothing to
        // do with them (fix-round item 2 measurement caught exactly this one layer down, in
        // ChatTranscript's per-item dispatch - this is the same instability's other end).
        val callbacks = remember(viewModel, onNotice) {
            ChatCallbacks(
                onSend = viewModel::send,
                onStop = viewModel::stop,
                onRetry = viewModel::retry,
                onNotice = onNotice,
                onReadAloudToggle = viewModel::toggleReadAloud,
                onHandoffDraftConsumed = viewModel::consumeHandoffDraft,
                onAttachTap = { composerOptionsOpen = true },
                onScopeIndicatorTap = { composerOptionsOpen = true },
                onRemoveAttachment = viewModel::removeStagedAttachment,
                onMicTap = {
                    if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        viewModel.startDictation()
                    } else {
                        recordAudioLauncher.launch(Manifest.permission.RECORD_AUDIO)
                    }
                },
                onDictationStop = viewModel::stopDictation,
                onDictationFinalConsumed = viewModel::consumeDictationFinal,
                onUserMessageLongPress = { messageActionTarget = it },
                onSubmitQuestionAnswer = viewModel::answerQuestion,
            )
        }
        // A plain Box, not applying `padding` itself - ChatSurface below keeps its own existing
        // `.padding(padding).fillMaxSize()`, and StopSpeakingControl applies that SAME padding
        // independently so the two land at the identical y-origin (flush under the header) without
        // one becoming a scroll-affected child of the other ("scroll-independent
        // overlay at the ChatScreen scaffold layer, NOT a chat row").
        Box(modifier = Modifier.fillMaxSize()) {
            ChatSurface(
                state = state,
                callbacks = callbacks,
                modifier = Modifier
                    .padding(padding)
                    .fillMaxSize(),
            )
            StopSpeakingControl(
                visible = state.speakingKey != null,
                onClick = viewModel::stopSpeaking,
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .padding(padding)
                    .padding(end = AuraSpacing.Composer.horizontalMargin),
            )
        }
        }
    }

    // Long-press actions on one user message (retry/branch/fork). Hosted HERE, alongside the model
    // picker and composer options, rather than inside ChatSurface: two of its three actions end in
    // NAVIGATION (a fork opens the new session), which is a screen-level concern the host-agnostic
    // surface has no business carrying. `running`/`sessionEnded` are read live, so the sheet's rows
    // withdraw under it if the session's state changes while it's open.
    messageActionTarget?.let { target ->
        // Both fork actions are the SAME call with a different cut point (`fromTs` = this message /
        // null = the whole transcript) and the same landing: tell the sheet whether it worked, then
        // navigate to whatever new session came back.
        val forkAndOpen: (String?, (Boolean) -> Unit) -> Unit = { fromTs, onResult ->
            viewModel.forkSession(fromTs) { newSessionId ->
                onResult(newSessionId != null)
                newSessionId?.let(onOpenSession)
            }
        }
        MessageActionsSheet(
            message = target,
            running = state.runPhase.isRunInFlight,
            sessionEnded = state.sessionEnded,
            onDismiss = { messageActionTarget = null },
            onRetryFromHere = { onResult -> viewModel.retryFromMessage(target, onResult) },
            onBranchInNewChat = { onResult -> forkAndOpen(target.ts, onResult) },
            onForkSession = { onResult -> forkAndOpen(null, onResult) },
            // The SAME clipboard idiom as the top bar's "Copy conversation" right above and the
            // assistant action row's own Copy — LocalClipboardManager + AnnotatedString + a "Copied"
            // notice. Synchronous, so it settles the sheet immediately.
            onCopy = { onResult ->
                clipboard.setText(AnnotatedString(target.text))
                onNotice("Copied")
                onResult(true)
            },
        )
    }

    if (modelPickerOpen) {
        ModelPickerSheet(
            models = state.models,
            selectedModel = state.selectedModel,
            onSelect = { id ->
                viewModel.selectModel(id)
                modelPickerOpen = false
            },
            onDismiss = { modelPickerOpen = false },
        )
    }

    if (composerOptionsOpen) {
        val models = state.models
        ComposerOptionsSheet(
            scope = state.composerScope,
            // freeze Project/Tools ONLY while a turn is actively in flight (the backend
            // re-resolves both from the running query's own body), NOT for the whole life of a created
            // session — an idle session's scope stays editable for the next turn (mirrors the freely
            // re-pickable ModelPickerSheet above).
            runInFlight = state.runPhase.isRunInFlight,
            visionSupported = models?.supportsVision(state.selectedModel ?: models.default) ?: false,
            onDismiss = { composerOptionsOpen = false },
            onPhotosTap = {
                photosLauncher.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
            },
            onFilesTap = { filesLauncher.launch(DOCUMENT_MIME_TYPES) },
            onSelectProject = viewModel::selectProject,
            onToggleTool = viewModel::toggleTool,
            onToggleServer = viewModel::toggleServer,
            onRefresh = { viewModel.refreshComposerScope(onNotice) },
        )
    }
}

/**
 * Pure bottom-glow state decision for [ChatScreen] (extracted for unit-testability of the
 * completion re-arm). A live run always wins → [EdgeGlowState.Thinking]. Otherwise the ambient
 * [EdgeGlowState.Listening] breathe shows when EITHER the fresh-invocation window is still open on a
 * still-empty transcript (the pre-existing "app just opened" linger, gated on an empty transcript so
 * a resumed conversation doesn't re-breathe) OR a run has just completed and its bounded linger is
 * armed (this second arm deliberately does NOT require an empty transcript: it fires precisely
 * because a response landed into a non-empty one). Everything else is solid rest ([EdgeGlowState.Hidden]).
 */
internal fun chatGlowState(
    runPhase: RunPhase,
    freshInvocationActive: Boolean,
    completionLingerActive: Boolean,
    transcriptEmpty: Boolean,
    loadingHistory: Boolean,
): EdgeGlowState = when {
    runPhase == RunPhase.Sending || runPhase == RunPhase.Streaming -> EdgeGlowState.Thinking
    (freshInvocationActive && transcriptEmpty && !loadingHistory) || completionLingerActive ->
        EdgeGlowState.Listening(0f)
    else -> EdgeGlowState.Hidden
}

/** `packages/mewbo_core/src/mewbo_core/attachments.py`'s `DOCUMENT_MIME_TYPES` - the Files picker
 * is restricted to this set (images go through the separate Photos picker, matching the reference
 * plus-menu's own Gallery-vs-Files split). */
/** In-app bottom-glow reach multiplier over the overlay's capture-measured decay length (user
 * directive: on the full app screen the glow must rise at least a bit above the composer area -
 * the overlay-measured reach reads tiny under the app's taller bottom stack). Behavioral tuning
 * constant, caller-side by design (ui/aurora/CLAUDE.md provenance rule). */
private const val IN_APP_GLOW_REACH_SCALE = 5.5f // raised 2.6->4.0->5.5 over three user rounds: the wave flows up through the center of the screen

/** Near-flat horizontal spread for the in-app bloom (user directive: cover the entire bottom edge
 * smoothly, "not a tick coming from the center") - the overlay keeps its state defaults, whose
 * center-anchoring is correct there (its glow radiates from the pill). */
private const val IN_APP_GLOW_CENTER_WEIGHT = 0.12f

/** Softer than the overlay's capture-measured peak (user directive: less opaque, smoother). */
private const val IN_APP_GLOW_ALPHA_SCALE = 0.65f

/** Slightly-faster wave drift for the ACTIVE (Sending/Streaming -> EdgeGlowState.Thinking) in-app
 * bottom glow only (user directive 2026-07-04: the processing/invocation liveness should read a
 * touch livelier, "speed of the Aura slightly visibly higher"; the idle chat stays as-is). Passed as
 * AuroraEdgeGlow's `speedScale` for the Thinking state; a caller-side behavioral tuning constant,
 * same provenance rule as the IN_APP_GLOW_* reach/center-weight/alpha knobs above. */
private const val IN_APP_GLOW_ACTIVE_SPEED_SCALE = 1.35f

/** How long the ambient bottom glow lingers on a fresh chat-screen invocation before fading to
 * Hidden (user directive 2026-07-04: resting state is a solid background - ambient glow only briefly
 * on fresh invocation, then only while running). A behavioral tuning constant.
 *
 * User directive 2026-07-14: the glow dimmed too quickly on a fresh session/screen — lengthened to
 * AT LEAST 3× the prior 10s window (10s → 30s), so the invocation aurora lingers noticeably before
 * settling to the solid resting state. Still only the FRESH-invocation ambient breathe; a live run's
 * Thinking glow and the ≥3s run-end ease-off are unaffected. */
private const val AMBIENT_INVOCATION_WINDOW_MS = 30_000L

private val DOCUMENT_MIME_TYPES = arrayOf(
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/csv",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/xml",
    "text/xml",
    "application/x-yaml",
    "text/yaml",
    "text/html",
)

/** Rev E-5's "Copy conversation" (top bar's ONE overflow item): user/assistant turns only - chips,
 * plan cards, and errors are UI-only artifacts, not conversation content. */
private fun formatTranscriptForCopy(items: List<ChatItem>): String = items.mapNotNull { item ->
    when (item) {
        is ChatItem.UserBubble -> "You: ${item.text}"
        is ChatItem.AssistantMessage -> "Mewbo: ${item.text}"
        else -> null
    }
}.joinToString("\n\n")

/**
 * Spec §6.1: two variants (at-rest transparent-over-canvas / over-wash with `surfaceIconScrim`
 * icon containers), auto-switching on [overWash] with a 200ms crossfade (reuses
 * [AuraMotion.composerMorphMs] - same 200ms value, no dedicated top-bar transition token exists).
 * NAMING RULE (Rev D-4): "Aura" stays the internal codename; every user-visible string here reads
 * "Mewbo".
 */
@Composable
private fun ChatTopBar(
    sessionOpen: Boolean,
    overWash: Boolean,
    modelDisplayName: String,
    pickerExpanded: Boolean,
    onMenuTap: () -> Unit,
    onNewChat: () -> Unit,
    onModelTap: () -> Unit,
    onCopyConversation: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var overflowExpanded by remember { mutableStateOf(false) }
    val chevronRotation by animateFloatAsState(
        targetValue = if (pickerExpanded) 180f else 0f,
        animationSpec = tween(AuraMotion.composerMorphMs),
        label = "model-picker-chevron",
    )

    // enableEdgeToEdge() (MainActivity) draws the whole window under the system bars, so this
    // custom row - not M3's real TopAppBar, which would apply WindowInsets.statusBars from
    // TopAppBarDefaults.windowInsets for free - has to consume the inset itself, same idiom
    // AssistOverlayScreen.kt already uses (systemBarsPadding()) for its own top-aligned chrome.
    // Scaffold's default contentWindowInsets only reaches the BODY padding, never the topBar slot.
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .statusBarsPadding()
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = TopBarVerticalPadding),
    ) {
        TopBarGlyphButton(icon = ChatIcons.TwoLineMenu, description = "Menu", overWash = overWash, onClick = onMenuTap)

        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(TitleGap),
            modifier = Modifier
                .weight(1f)
                .clickable(onClick = onModelTap)
                .padding(horizontal = AuraSpacing.Composer.gapTight),
        ) {
            Text(text = "Mewbo", style = AuraType.titleBar, color = AuraColors.textPrimary)
            Text(text = modelDisplayName, style = AuraType.titleBar.copy(fontWeight = FontWeight.Normal, color = AuraColors.textSecondary))
            Icon(
                imageVector = Icons.Filled.KeyboardArrowDown,
                contentDescription = null,
                tint = AuraColors.textSecondary,
                modifier = Modifier
                    .size(ChevronSize)
                    .graphicsLayer { rotationZ = chevronRotation },
            )
        }

        TopBarGlyphButton(icon = Icons.Filled.Edit, description = "New chat", overWash = overWash, onClick = onNewChat)

        if (sessionOpen) {
            Box {
                TopBarGlyphButton(icon = Icons.Filled.MoreVert, description = "More", overWash = overWash, onClick = { overflowExpanded = true })
                ChatOverflowMenu(
                    expanded = overflowExpanded,
                    onDismiss = { overflowExpanded = false },
                    items = listOf(
                        OverflowMenuItem(icon = ChatIcons.ContentCopy, label = "Copy conversation") {
                            overflowExpanded = false
                            onCopyConversation()
                        },
                    ),
                )
            }
        }
    }
}

@Composable
private fun TopBarGlyphButton(
    icon: ImageVector,
    description: String,
    overWash: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val scrimColor by animateColorAsState(
        targetValue = if (overWash) AuraColors.surfaceIconScrim else Color.Transparent,
        animationSpec = tween(AuraMotion.composerMorphMs),
        label = "top-bar-icon-scrim",
    )
    Box(
        modifier = modifier
            .minimumInteractiveComponentSize()
            .background(scrimColor, CircleShape)
            .clickable(onClick = onClick),
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

/** Spec §6.1: "6dp gaps" / "20dp chevron" - no matching tokens; flagged in the task report. */
private val TitleGap = 6.dp
private val ChevronSize = 20.dp
private val TopBarVerticalPadding = 8.dp

/**
 * two-widget pattern's SECOND widget - a sticky control pinned flush under the
 * header, distinct from the per-message [ReadAloudButton] row control (which keeps its own
 * existing states unchanged). Visible only while [ChatUiState.speakingKey] is non-null, whether
 * that's a P3 speak-along or a P4 manual read-aloud - [ChatViewModel.stopSpeaking] doesn't
 * distinguish the two either, so neither does this control. Tokens only, no new one needed: the
 * reference app's capture (2026-07-03 comment) measured this at exactly a 48dp square
 * touch target with a 16dp right margin - already [AuraSpacing.ActionRow.cellSize] and
 * [AuraSpacing.Composer.horizontalMargin] verbatim. The appear/disappear crossfade reuses the M8
 * "flat fade" token ([AuraMotion.reducedBlockFadeMs]) rather than [AuraMotion.actionRowFadeMs]'s own
 * fade+slide (the row action row's own entrance) - the reference shows a plain fade here, no slide.
 */
@Composable
private fun StopSpeakingControl(visible: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    AnimatedVisibility(
        visible = visible,
        enter = fadeIn(tween(AuraMotion.reducedBlockFadeMs)),
        exit = fadeOut(tween(AuraMotion.reducedBlockFadeMs)),
        modifier = modifier,
    ) {
        Box(
            modifier = Modifier
                .size(AuraSpacing.ActionRow.cellSize)
                .background(AuraColors.surfaceIconScrim, RoundedCornerShape(AuraShape.radiusThumb))
                .clickable(onClick = onClick),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                imageVector = ChatIcons.Stop,
                contentDescription = "Stop reading aloud",
                tint = AuraColors.accentPrimary,
                modifier = Modifier.size(AuraSpacing.ActionRow.iconSize),
            )
        }
    }
}
