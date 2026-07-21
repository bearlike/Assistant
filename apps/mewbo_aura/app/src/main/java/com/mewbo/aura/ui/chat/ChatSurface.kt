package com.mewbo.aura.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.Icon
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.isSpecified
import androidx.compose.material3.Text
import com.mewbo.aura.data.model.ComposerScope
import com.mewbo.aura.ui.aurora.AuroraState
import com.mewbo.aura.ui.composer.AuraComposer
import com.mewbo.aura.ui.composer.ComposerState
import com.mewbo.aura.ui.orb.AuraSpark
import com.mewbo.aura.ui.orb.SparkState
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.voice.InputModality
import kotlinx.coroutines.delay

/**
 * Host-agnostic chat core: greeting-or-transcript + composer, nothing else (apps/mewbo_aura/CLAUDE.md
 * "One chat composable tree, two hosts"). [ChatScreen] wraps this with top-bar chrome; a later
 * assist-overlay host wraps the same composable with its own chrome instead.
 *
 * The aurora wash (spec §3.3/M3/M4) and the greeting-vs-transcript swap both key off [ChatUiState.runPhase]
 * / [ChatUiState.items] directly rather than a separate boolean - [ChatScreen]'s top-bar variant
 * derives the SAME "is wash active" fact from `state.runPhase` independently, so the two stay in
 * sync without threading a shared flag between them.
 */
@Composable
fun ChatSurface(
    state: ChatUiState,
    callbacks: ChatCallbacks,
    modifier: Modifier = Modifier,
) {
    var draft by rememberSaveable(stateSaver = TextFieldValue.Saver) { mutableStateOf(TextFieldValue()) }
    // which modality the NEXT send should be tagged with - Voice once a dictation
    // Final/stop has landed text in the field, reset to Text by any subsequent REAL keystroke (the
    // onDraftChange callback below - never touched by the LaunchedEffect's own programmatic fill,
    // which assigns `draft` directly). Ephemeral by design, matching InputModality's own accepted
    // "not persisted across process death" posture - a plain `remember`, not `rememberSaveable`.
    var pendingModality by remember { mutableStateOf(InputModality.Text) }
    val dictation = state.dictation
    val isRunning = state.runPhase == RunPhase.Sending || state.runPhase == RunPhase.Streaming
    val composerState = ComposerState.resolve(
        draftText = draft.text,
        isDictating = dictation is DictationState.Listening,
        isStreaming = isRunning,
        rmsDb = (dictation as? DictationState.Listening)?.rmsDb ?: 0f,
        partialText = (dictation as? DictationState.Listening)?.partial,
    )

    // One-shot: a Final (a genuine recognizer Final OR ChatViewModel.stopDictation's "keep the
    // partial" path - see DictationState's own KDoc) drops its text into the now-editable field and
    // tags the pending send Voice, then acks back so it never replays on a later recomposition.
    LaunchedEffect(dictation) {
        val finalText = (dictation as? DictationState.Final)?.text ?: return@LaunchedEffect
        draft = TextFieldValue(finalText, selection = TextRange(finalText.length))
        pendingModality = InputModality.Voice
        callbacks.onDictationFinalConsumed()
    }

    // One-shot: a pull-up handoff draft (assist overlay pill swiped up into the app, user directive
    // 2026-07-04) lands in the editable field exactly like a dictation Final, EXCEPT it never tags
    // the pending send Voice - the text was typed, and a masqueraded Voice tag would trigger
    // speak-along on the reply. Acked back so it can't replay on a later recomposition.
    LaunchedEffect(state.pendingHandoffDraft) {
        val text = state.pendingHandoffDraft ?: return@LaunchedEffect
        draft = TextFieldValue(text, selection = TextRange(text.length))
        callbacks.onHandoffDraftConsumed()
    }

    // P0 reference capture: a transient "tap Stop or Send when you're done speaking" hint shown on
    // ENTERING dictation, auto-dismissing on its own short timer - listening itself continues
    // unaffected by the hint's own visibility.
    var showDictationHint by remember { mutableStateOf(false) }
    LaunchedEffect(dictation is DictationState.Listening) {
        if (dictation is DictationState.Listening) {
            showDictationHint = true
            delay(DictationHintDurationMs)
            showDictationHint = false
        } else {
            showDictationHint = false
        }
    }

    // Aura liveness lives in ChatScreen's BOTTOM edge glow only, processing/invocation-gated - never
    // render a resting/top wash here (user directive 2026-07-04, reverted regression 65c4023). This
    // derivation stays ONLY to feed ChatTranscript's `overWash` styling; it drives no wash layer.
    val auroraState = when (state.runPhase) {
        RunPhase.Sending -> AuroraState.Thinking
        RunPhase.Streaming -> AuroraState.Streaming
        RunPhase.Idle, RunPhase.Done, RunPhase.Error -> AuroraState.Hidden
    }
    val washActive = auroraState != AuroraState.Hidden
    val showGreeting = state.items.isEmpty() && !state.isLoadingHistory

    // imePadding: the activity is edge-to-edge, so adjustResize alone never lifts the composer -
    // without this the keyboard fully occludes it (found in the W2 on-device gate).
    Column(modifier = modifier.fillMaxSize().imePadding()) {
        Box(modifier = Modifier.weight(1f).fillMaxWidth()) {
            // The aurora wash itself is NOT rendered here anymore (user report):
            // inside the Scaffold BODY it could never extend under the top bar, so the header row
            // sat on a solid containerColor band - the reference landing runs the wash beneath the
            // header and status bar. ChatScreen hosts the wash behind a transparent Scaffold now;
            // this file keeps only the derivation feeding [ChatTranscript]'s overWash styling.
            if (showGreeting) {
                GreetingScreen(
                    displayName = state.displayName,
                    isOffline = state.isOffline,
                    modifier = Modifier.fillMaxSize(),
                )
            } else {
                ChatTranscript(
                    items = state.items,
                    runPhase = state.runPhase,
                    sessionId = state.sessionId,
                    onRetry = callbacks.onRetry,
                    sessionEnded = state.sessionEnded,
                    speakingKey = state.speakingKey,
                    onNotice = callbacks.onNotice,
                    onReadAloudToggle = callbacks.onReadAloudToggle,
                    onUserMessageLongPress = callbacks.onUserMessageLongPress,
                    onSubmitQuestionAnswer = callbacks.onSubmitQuestionAnswer,
                    isLoadingHistory = state.isLoadingHistory,
                    overWash = washActive,
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }

        if (state.sessionEnded) {
            // The session is permanently terminated: the composer is disabled below
            // (`enabled = false`) and this quiet caption carries the reason. AuraComposer's own
            // "Ask Mewbo" placeholder is internal to ui/composer and has no override hook, so the
            // message lives here rather than in the field. Calm by design - a terminal state is not
            // a failure, so no warning glyph and no accentError (DESIGN.md §6: no error residue).
            Text(
                text = "This session was ended and can't continue",
                style = AuraType.caption,
                textAlign = TextAlign.Center,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = AuraSpacing.AssistantText.gutter, vertical = AuraSpacing.Composer.gapTight),
            )
        }

        // Pre-session only (no sessionId / no items yet) - once a real turn is in flight the
        // options sheet's own frozen-scope rows (ComposerOptionsSheet) are the source of truth for
        // "what this chat is scoped to," not a second indicator floating above the composer.
        if (state.sessionId == null && state.items.isEmpty()) {
            // No vertical padding here: the row's own heightIn(min = 48dp a11y target) plus the
            // composer's 12dp top inset already separate it; stacking more would read as a chunky band.
            ComposerScopeIndicator(
                scope = state.composerScope,
                onClick = callbacks.onScopeIndicatorTap,
            )
        }

        AnimatedVisibility(visible = showDictationHint) {
            Text(
                text = "Tap Stop or Send when you're done speaking",
                style = AuraType.caption,
                textAlign = TextAlign.Center,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = AuraSpacing.Composer.horizontalMargin, vertical = AuraSpacing.Composer.gapTight),
            )
        }

        AuraComposer(
            state = composerState,
            draft = draft,
            onDraftChange = { newValue ->
                draft = newValue
                pendingModality = InputModality.Text
            },
            onSend = {
                val toSend = draft.text
                if (toSend.isNotBlank()) {
                    draft = TextFieldValue()
                    val modality = pendingModality
                    pendingModality = InputModality.Text
                    callbacks.onSend(toSend, modality)
                }
            },
            onStop = callbacks.onStop,
            // ChatScreen wires real dictation - the permission-gated
            // startDictation() call itself lives there; a host that hasn't wired it yet (fix-round-3
            // Important #4's honest-fallback convention) falls through to a notice, not a dead tap.
            onMicTap = { callbacks.onMicTap?.invoke() ?: callbacks.onNotice("Dictation: coming to this surface") },
            onDictationStop = { callbacks.onDictationStop?.invoke() },
            // A full voice turn needs AssistTurnMachine's own lifecycle (voice/, overlay-scoped) -
            // rebuilding that here would duplicate a whole turn machine (brief: KISS, don't). Falls
            // through to a notice unless a host (e.g. a future overlay reuse of ChatSurface) wires
            // the real seam via ChatCallbacks.onVoiceModeTap.
            onVoiceModeTap = { callbacks.onVoiceModeTap?.invoke() ?: callbacks.onNotice("Voice mode: hold the assistant gesture") },
            onAttachTap = callbacks.onAttachTap,
            stagedAttachments = state.stagedAttachments,
            onRemoveAttachment = callbacks.onRemoveAttachment,
            enabled = !state.isOffline && !state.sessionEnded,
            micEnabled = !state.isOffline && !state.sessionEnded && state.dictationAvailable,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.Composer.horizontalMargin, vertical = AuraSpacing.Composer.bottomInset),
        )
    }
}

/** capture: how long the dictation-entry hint stays visible before auto-
 * dismissing on its own - no matching [AuraSpacing]/duration token exists for this one-shot coach
 * mark (same convention this file's [GreetingTopWeight] already established for a genuinely missing
 * token). */
private const val DictationHintDurationMs = 1_500L

/**
 * The empty new-chat state (spec §6.6). Content sits with its TOP edge at [GreetingTopWeight] of
 * the available height (a weighted-Spacer split - a close, simple approximation of the spec's "~42%
 * height" CENTER position without needing exact content-height math for a subjective placement).
 */
@Composable
private fun GreetingScreen(displayName: String, isOffline: Boolean, modifier: Modifier = Modifier) {
    Column(modifier = modifier) {
        Spacer(modifier = Modifier.weight(GreetingTopWeight))
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.screenGutter),
        ) {
            AuraSpark(state = SparkState.Shimmer, size = AuraSpacing.Greeting.sparkSize)
            Spacer(modifier = Modifier.height(AuraSpacing.Greeting.gapBelowSpark))
            Text(
                text = if (displayName.isBlank()) "Let's get into it" else "Hi $displayName,\nlet's get into it",
                style = AuraType.greetingDisplay,
                color = AuraColors.textPrimary,
                textAlign = TextAlign.Center,
            )
            if (isOffline) {
                Spacer(modifier = Modifier.height(AuraSpacing.Composer.internalPadding))
                // Spec §6.12-style quiet block - replaces the old OfflineBanner (deleted).
                Text(
                    text = "No backend configured — open Settings",
                    style = AuraType.caption,
                    textAlign = TextAlign.Center,
                )
            }
        }
        Spacer(modifier = Modifier.weight(1f - GreetingTopWeight))
    }
}

private const val GreetingTopWeight = 0.42f

/**
 * Pre-session project/tool scope, tappable through to the composer options sheet
 * (icons/colors/facets user directive 2026-07-14). A leading project glyph
 * ([ChatIcons.ProjectScope], tinted [AuraColors.scopeProject]) before the project name, then a
 * tools glyph ([ChatIcons.ToolScope], tinted [AuraColors.scopeTool]) before a provenance-faceted
 * summary ("2 project · 5 system" — [ComposerScope.toolsFacetSummary]). The tools half appears only
 * once the catalog resolves (`toolsFacetSummary` non-null), so the row is `<glyph> <project>` alone
 * until then — no spinner, no placeholder count. When no real project is selected the leading glyph
 * switches to the ephemeral [ChatIcons.TemporaryProjectScope] (a muted tint) so "Temporary" reads as
 * the throwaway scratch cwd it is. Left-inset to [AuraSpacing.Composer.scopeRowStartInset] (aligns
 * with the composer pill's straight-edge start); both edges sit well within the screen.
 *
 * **Truncation is a last resort.** The row previously gave both chips an equal
 * `Modifier.weight(1f, fill = false)`, which caps EACH chip's max width at exactly half of whatever
 * space is left over — regardless of what either chip's content actually needs. A short project name
 * ("Temporary") left half its share unused while a long facet breakdown ("111 system · 31 plugin")
 * was still hard-capped at its own half and ellipsized, even though the row had plenty of total width.
 * [BoxWithConstraints] + a one-shot [rememberTextMeasurer] pre-measurement replace that fixed split
 * with a content-aware, three-rung ladder, decided ONCE per available width / content change (no
 * flicker, no custom [androidx.compose.ui.layout.Layout]):
 * 1. Both chips render at their natural width when [ComposerScope.toolsFacetSummary] fits alongside
 *    the project name in the available width — neither chip reserves space it doesn't need.
 * 2. Otherwise the facet breakdown collapses to [ComposerScope.toolsCompactSummary] ("142 tools") —
 *    strictly BEFORE any ellipsis.
 * 3. Only if even the compact total doesn't leave room for the project name does the project name's
 *    own `maxLines = 1` / [TextOverflow.Ellipsis] engage, capped to whatever the compact total left.
 */
@Composable
private fun ComposerScopeIndicator(scope: ComposerScope, onClick: () -> Unit, modifier: Modifier = Modifier) {
    val temporary = scope.selectedProjectKey == null
    BoxWithConstraints(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = AuraSpacing.ActionRow.cellSize) // ≥48dp a11y touch target for the whole tap
            .clickable(onClickLabel = "Edit project and tools scope", onClick = onClick)
            .padding(start = AuraSpacing.Composer.scopeRowStartInset, end = AuraSpacing.screenGutter),
    ) {
        val density = LocalDensity.current
        val textMeasurer = rememberTextMeasurer()
        val fullFacet = scope.toolsFacetSummary
        val compactFacet = scope.toolsCompactSummary
        val projectName = scope.projectDisplayName

        val (facetLabel, projectMaxWidth) = remember(projectName, fullFacet, compactFacet, maxWidth, density) {
            if (fullFacet == null) {
                return@remember null to Dp.Unspecified
            }
            // Fixed chrome: both glyphs + the three gaps between glyph/text/glyph/text. Everything
            // else in the row is negotiable between the two text chips.
            val chromePx = with(density) {
                (AuraSpacing.Composer.scopeRowIconSize * 2 + AuraSpacing.Composer.gapTight * 2 + AuraSpacing.Composer.internalPadding).toPx()
            }
            val availablePx = with(density) { maxWidth.toPx() }
            val projectPx = textMeasurer.measure(projectName, AuraType.chipLabel).size.width
            val fullFacetPx = textMeasurer.measure(fullFacet, AuraType.chipLabel).size.width
            if (chromePx + projectPx + fullFacetPx <= availablePx) {
                // Rung 1: both fit at natural width.
                fullFacet to Dp.Unspecified
            } else {
                // Rung 2: collapse the facet breakdown to its compact total first.
                val compactFacetPx = textMeasurer.measure(compactFacet.orEmpty(), AuraType.chipLabel).size.width
                if (chromePx + projectPx + compactFacetPx <= availablePx) {
                    compactFacet to Dp.Unspecified
                } else {
                    // Rung 3 (last resort): the compact total still doesn't leave room for the full
                    // project name - cap the project chip to whatever's left so ITS OWN maxLines=1 +
                    // TextOverflow.Ellipsis ellipsizes it, never the already-compacted facet chip.
                    val projectBudgetPx = (availablePx - chromePx - compactFacetPx).coerceAtLeast(0f)
                    compactFacet to with(density) { projectBudgetPx.toDp() }
                }
            }
        }

        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Icon(
                imageVector = if (temporary) ChatIcons.TemporaryProjectScope else ChatIcons.ProjectScope,
                contentDescription = null, // the project name beside it carries the meaning for TalkBack
                tint = if (temporary) AuraColors.textSecondary else AuraColors.scopeProject,
                modifier = Modifier.size(AuraSpacing.Composer.scopeRowIconSize),
            )
            Spacer(modifier = Modifier.width(AuraSpacing.Composer.gapTight))
            Text(
                text = projectName,
                style = AuraType.chipLabel,
                color = AuraColors.textSecondary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = if (projectMaxWidth.isSpecified) Modifier.widthIn(max = projectMaxWidth) else Modifier,
            )
            facetLabel?.let { label ->
                Spacer(modifier = Modifier.width(AuraSpacing.Composer.internalPadding))
                Icon(
                    imageVector = ChatIcons.ToolScope,
                    contentDescription = null, // the summary beside it carries the meaning for TalkBack
                    tint = AuraColors.scopeTool,
                    modifier = Modifier.size(AuraSpacing.Composer.scopeRowIconSize),
                )
                Spacer(modifier = Modifier.width(AuraSpacing.Composer.gapTight))
                Text(
                    text = label,
                    style = AuraType.chipLabel,
                    color = AuraColors.textSecondary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}
