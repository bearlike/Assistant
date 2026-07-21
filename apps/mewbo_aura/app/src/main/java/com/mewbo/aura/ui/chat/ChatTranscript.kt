package com.mewbo.aura.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyItemScope
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.api.QuestionAnswerItemDto
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.chat.toolcards.ToolCardRegistry
import com.mewbo.aura.ui.chat.widget.WidgetCard
import com.mewbo.aura.ui.chat.widget.WidgetSummaryCard
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.orb.AuraSpark
import com.mewbo.aura.ui.orb.SparkState
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.ui.theme.LocalAssistantExtras
import kotlinx.coroutines.launch

/**
 * The scrollable transcript (ui/CLAUDE.md "`LazyColumn(reverseLayout = true)`; stick-to-bottom
 * unless user scrolled up"). Items come only from [items] (the reducer's own output plus any
 * client-only tail from [ChatViewModel]) - never hand-assembled here. Stable [ChatItem.key]s mean
 * an in-flight delta only recomposes its own row; the rest of the list is skipped by Compose.
 *
 * An empty-and-not-loading transcript renders NOTHING (no fallback text) - [ChatSurface] swaps in
 * the greeting screen (spec §6.6) entirely instead of mounting this composable, so there's no
 * "which empty state wins" ambiguity to resolve here.
 *
 * [fillParent] (default `true` - byte-equivalent to the pre-v5 signature, the
 * docked in-app [ChatSurface] host never passes it): the internal `LazyColumn` normally forces
 * itself to fill whatever bounds [modifier] hands the outer `Box`, which is correct for a
 * fullscreen chat tree but wrong for the assist overlay's `ResponseCard`,
 * which needs to shrink-wrap a short reply and only grow (then internally scroll) up to its own
 * `heightIn(max = ...)` cap - reference-app-parity ground truth shows a one-line reply rendering at
 * ~90dp, not a fixed slab. `false` drops the internal `fillMaxSize()` down to `fillMaxWidth()`
 * only, letting `LazyColumn`'s own default measure policy size to its laid-out content within
 * whatever bounded (but not forced-fill) height constraint the CALLER's `modifier` supplies
 * (`ResponseCard`'s own `heightIn(max = ...)`) - reverseLayout/stick-to-bottom is unaffected by
 * sizing mode either way.
 */
@Composable
fun ChatTranscript(
    items: List<ChatItem>,
    runPhase: RunPhase,
    onRetry: () -> Unit,
    sessionEnded: Boolean,
    speakingKey: String?,
    onNotice: (String) -> Unit,
    onReadAloudToggle: (ChatItem.AssistantMessage) -> Unit,
    modifier: Modifier = Modifier,
    isLoadingHistory: Boolean = false,
    overWash: Boolean = false,
    fillParent: Boolean = true,
    // `true` (docked chat) renders a widget in its interactive stlite WebView;
    // `false` (the assist overlay) renders the compact summary card instead - booting Pyodide in a
    // small floating overlay is wrong. A dedicated flag rather than reusing [fillParent] (its
    // sizing concern is orthogonal), extending this param surface the way did rather than
    // forking the composable (ui/CLAUDE.md).
    allowRichWidgets: Boolean = true,
    // The overlay's summary card taps through to here (its expand/handoff), opening the interactive
    // widget on the chat surface. Default no-op: the docked chat renders the interactive card itself.
    onOpenWidgetInApp: () -> Unit = {},
    // Defaults null so ui/overlay's OWN direct ChatTranscript call site (ResponseSheet, out of
    // this lane) keeps compiling unchanged - the M7-replay-on-rebind fix below only matters for
    // ChatSurface's session-switching host, which passes the real id explicitly.
    sessionId: String? = null,
    // Long-press a user bubble -> the host opens MessageActionsSheet on it (retry/branch/fork).
    // Nullable, defaulting to null, for the same reason [sessionId] does: the assist overlay renders
    // a single fresh turn it can neither rewind nor fork, so it wires nothing and its bubbles keep
    // their select-to-copy long-press untouched. Whether a GIVEN bubble actually gets the gesture is
    // [MessageAction.availableFor]'s call, applied per row in [ChatItemRow].
    onUserMessageLongPress: ((ChatItem.UserBubble) -> Unit)? = null,
    // Submit a ChatItem.Question card's answer (ask-user questions). Default no-op so the assist
    // overlay's own ChatTranscript call site (out of this lane) keeps compiling — it hands off to the
    // app before a blocked question is answered. The docked ChatSurface host passes the real callback.
    onSubmitQuestionAnswer: (callId: String, callToken: String, answers: List<QuestionAnswerItemDto>, onResult: (Boolean) -> Unit) -> Unit = { _, _, _, onResult -> onResult(false) },
) {
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val reversedItems = remember(items) { items.asReversed() }
    // one shared per-item transition (fades + a placement spring) smooths the abrupt
    // pop-in/shuffle/pop-out of live-turn content into a calm reflow; dropped to snap+flat-fade
    // under reduced motion. Read once here and threaded into `transcriptItemTransition` per row.
    val reducedMotion = LocalAssistantExtras.current.reducedMotion

    val isAtBottom by remember {
        derivedStateOf { listState.firstVisibleItemIndex == 0 && listState.firstVisibleItemScrollOffset <= STICK_THRESHOLD_PX }
    }

    // A ToolCallGroup auto-expands only while it's still the most recent thing in the transcript
    // AND the run itself is live - the moment an assistant reply (or anything
    // else) lands after it, or the run ends, it's no longer "the last item" and should read as
    // settled history, not in-flight work.
    val isRunLive = runPhase == RunPhase.Sending || runPhase == RunPhase.Streaming

    // The AuraSpark row is the PERSISTENT run-liveness cue (user directive 2026-07-04: "must always
    // be able to tell running from dead"), visible for the entire run - not just the pre-first-delta
    // gap. Follow-up sends and mid-turn tool phases (nothing else on screen moves during those) need
    // the same tell the original Sending-only state gave, so this is simply isRunLive rather than
    // additionally gating on whether an assistant message has opened yet (the old `hasOpenAssistant`
    // check, now unused and removed).
    val showThinking = isRunLive

    // Transcript-level fact a single row can't determine about itself (spec §6.5/§6.12): does ANY
    // settled assistant reply exist yet? Computed once per items-list change, not per row. The
    // per-row action-row footer is a separate, purely per-item fact (see the itemsIndexed loop):
    // every COMPLETED assistant message now shows its own footer (user directive 2026-07-04 round 3:
    // footer under every response, not just the last), so there is no single "chosen" row to track.
    val hasSettledReply = remember(items) {
        items.any { it is ChatItem.AssistantMessage && !it.isStreaming }
    }

    // the bottom disclaimer renders ONLY once a reply has settled AND no run is live — it
    // must not mount during Sending/Streaming. On a FOLLOW-UP turn a prior reply is already settled
    // (`hasSettledReply` true), so without the `!isRunLive` guard the disclaimer stayed pinned at the
    // transcript bottom right under the fresh user bubble + spark, AHEAD of the new turn's response
    // and its footer (the reported overlap). `!isRunLive` is the transcript-level twin of the per-row
    // `showActionRow` (= a settled, non-streaming assistant message): the disclaimer's appearance now
    // coincides EXACTLY with the settled ActionRow footer's, never before it. This also makes the
    // disclaimer and the spark mutually exclusive (spark ⇔ live, disclaimer ⇔ settled), which is why
    // the spark below no longer needs a bottom inset to hold it off (supersedes §7.20).
    val showDisclaimer = shouldShowDisclaimer(hasSettledReply, isRunLive)

    // M7 chip entrance (spec §6.11: "Chips fade in per M7 timing when their event arrives") -
    // seeded from whatever's already SETTLED before animation should start, so only items that
    // arrive AFTER that point animate in. Deliberately a plain remembered MutableSet, not a
    // mutableStateOf: it lives in ChatTranscript's own composition scope (so it survives an
    // item's own scroll-driven decompose/recompose - a chip scrolled back into view never
    // replays the animation), and NEVER being read as Compose State means growing it can't
    // trigger every other row to recompose too (which a shared State-backed set would).
    //
    // Keyed on [sessionId], not a bare remember{} (fix-round-3 minor #5): ChatTranscript's OWN
    // composition survives a session switch (ChatViewModel/ChatScreen persist across the single
    // `chat` nav destination's rebinds, task integ bug A), so a one-shot seed would keep treating
    // a NEWLY bound session's whole reloaded history as "never seen" and replay every chip's
    // entrance every time the user opens it. A fresh empty set per session isn't enough by
    // itself, though: `bind()` resets `items` to empty and THEN loads history asynchronously, so
    // seeding immediately at rebind would capture nothing, and every historical chip would arrive
    // "new" one at a time as history replays in. The LaunchedEffect below instead captures the
    // baseline once loading has genuinely SETTLED (`isLoadingHistory` back to false, by which
    // point `bind()`'s history fold has already populated `items` with the full backlog) -
    // anything that arrives after that point is a real live event, not backlog.
    val seenKeys = remember(sessionId) { mutableSetOf<String>() }
    LaunchedEffect(sessionId, isLoadingHistory) {
        if (!isLoadingHistory) seenKeys.addAll(items.map { it.key })
    }

    // Stick-to-bottom: index 0 is always the visual-bottom row under reverseLayout. The disclaimer
    // and the spark are now mutually exclusive (disclaimer ⇔ settled, spark ⇔ live), so exactly one
    // of them (or, mid-first-stream, the newest content) occupies DSL position 0 → rendered index 0,
    // and scrolling to 0 always lands on the tail. isAtBottom's `firstVisibleItemIndex == 0` test
    // stays correct. The disclaimer's appearance (showDisclaimer flipping) coincides with a run
    // settling — i.e. an isRunLive/showThinking change — so it is already covered by these keys.
    LaunchedEffect(reversedItems.size, showThinking) {
        if (isAtBottom) listState.animateScrollToItem(0)
    }

    Box(modifier = modifier) {
        if (items.isEmpty() && isLoadingHistory) {
            CircularProgressIndicator(modifier = Modifier.align(Alignment.Center), color = AuraColors.textSecondary)
        }

        LazyColumn(
            state = listState,
            reverseLayout = true,
            modifier = if (fillParent) Modifier.fillMaxSize() else Modifier.fillMaxWidth(),
            contentPadding = PaddingValues(vertical = AuraSpacing.Composer.gapTight),
            // No uniform Arrangement.spacedBy - spec §6.3's rhythm is ASYMMETRIC (24dp after a
            // user bubble, 12dp otherwise), computed per item below instead.
        ) {
            // The "Mewbo is an AI tool and can make mistakes." disclaimer anchors to the END of the whole transcript
            // - one instance, final position only (user directive 2026-07-04 round 2; supersedes
            // the first-turn anchoring from earlier the same day, which showed it mid-conversation
            // above a turn-boundary divider). Being its OWN keyed item declared at DSL position 0,
            // reverseLayout renders it at the visual BOTTOM - below everything: the newest response
            // and its action-row footer - and it stays there as new turns append (new content has
            // higher reversedItems indices, so it renders ABOVE this fixed row). Gated on
            // `showDisclaimer` (= a reply has settled AND no run is live): it never shows
            // during the first turn's stream, and — the fix — never during a FOLLOW-UP turn's stream
            // either, where it would sit under the fresh user bubble ahead of the new response. So it
            // is mutually exclusive with the spark below (settled ⇔ disclaimer, live ⇔ spark), and its
            // appearance coincides with the settled ActionRow footer's. Index 0 stays the
            // visual-bottom stick target, so autoscroll/isAtBottom above are unaffected. animateItem
            // fades it in/out on that gated appearance rather than popping.
            if (showDisclaimer) {
                item(key = "disclaimer") {
                    Text(
                        text = "Mewbo is an AI tool and can make mistakes.",
                        style = AuraType.caption,
                        modifier = transcriptItemTransition(reducedMotion, fadeEdges = true)
                            .fillMaxWidth()
                            .padding(horizontal = AuraSpacing.AssistantText.gutter)
                            .padding(top = AuraSpacing.ActionRow.disclaimerGap),
                    )
                }
            }
            if (showThinking) {
                item(key = "thinking-indicator") {
                    Box(
                        modifier = transcriptItemTransition(reducedMotion, fadeEdges = true)
                            .fillMaxWidth()
                            .padding(horizontal = AuraSpacing.AssistantText.gutter)
                            // The spark is the bottom-most row of a LIVE turn: the disclaimer no
                            // longer co-renders during Sending/Streaming (they are now
                            // mutually exclusive), so it only needs a top gap against the streamed
                            // text above it; the LazyColumn's own bottom contentPadding buffers it off
                            // the composer. The old `bottom = Composer.gapTight` inset was §7.20
                            // compensation to hold the disclaimer off the spark — dead now that the
                            // disclaimer is never its neighbor during a live turn.
                            .padding(top = AuraSpacing.ActionRow.topMargin),
                        contentAlignment = Alignment.CenterStart,
                    ) {
                        // M3 (spec §7): AuraSpark's Thinking pulse replaces the old 3-dot
                        // TypingIndicator. Per showThinking's own KDoc above, it's the persistent
                        // run-liveness cue for the WHOLE run now, not just the pre-first-delta gap.
                        AuraSpark(state = SparkState.Thinking, size = AuraSpacing.Composer.iconSize)
                    }
                }
            }
            itemsIndexed(reversedItems, key = { _, item -> item.key }) { index, item ->
                // reverseLayout renders index 0 at the bottom, growing upward - the item ONE
                // INDEX HIGHER in `reversedItems` is this one's chronological predecessor, i.e.
                // the neighbor rendered directly above it (spec §5: "top gap after bubble 24dp;
                // paragraph gap 12dp" - the 24dp applies when THAT neighbor is a user bubble).
                val precedingItem = reversedItems.getOrNull(index + 1)
                // Chip-family cards (ToolCallGroup/TodoList/AgentChip/ToolCard) are solid surfaces
                // with no intrinsic leading to absorb into, unlike bare assistant text, so they read
                // their gap-after-a-bubble off ActivityGroup rather than AssistantText (whose token
                // is tuned assuming the font's own leading already contributes ink distance). This
                // was the "chips sit 0dp under the bubble" bug. The two tokens
                // happen to hold the SAME value today (both 32dp, DESIGN.md §3 [R3]) - the branch is
                // kept because they are separate tokens by design and will diverge again, not
                // because the values differ right now. ToolCard is not a chip VISUALLY (it's a
                // payload card, not a 36dp glance row) but it is one for SPACING, which is all this
                // predicate decides - a promoted tool is very often the first thing under a bubble.
                val isChipFamily = item is ChatItem.ToolCallGroup || item is ChatItem.TodoList ||
                    item is ChatItem.AgentChip || item is ChatItem.ToolCard || item is ChatItem.Widget ||
                    item is ChatItem.Question
                val topGap = when {
                    isChipFamily && precedingItem is ChatItem.UserBubble -> AuraSpacing.ActivityGroup.topGapAfterBubble
                    precedingItem is ChatItem.UserBubble -> AuraSpacing.AssistantText.topGapAfterBubble
                    else -> AuraSpacing.AssistantText.paragraphGap
                }
                // A UserBubble with a chronological predecessor marks a NEW turn's start - the
                // reducer guarantees activity items (ToolCallGroup/AgentChip/TodoList) precede a
                // turn's assistant text and that text is the turn's LAST item (TranscriptReducer
                // invariant, data/CLAUDE.md), so every such boundary cleanly brackets one whole
                // turn against the one before it. The divider structure below REPLACES topGap for
                // this item - the two are never stacked.
                val isTurnBoundary = item is ChatItem.UserBubble && precedingItem != null
                // Set.add() returns true only the FIRST time this key is added; remember(item.key)
                // evaluates it exactly once per composition of this row and caches the result, so
                // re-executing this lambda (e.g. an unrelated recomposition) can't flip it back.
                val isNewlyArrived = remember(item.key) { seenKeys.add(item.key) }
                // See isRunLive's own KDoc above - "last item" here means chronologically last,
                // i.e. reverseLayout's index 0 (the bottom-most, most-recently-rendered row).
                val isToolGroupActive = index == 0 && isRunLive
                Column(
                    // fadeEdges=false — chip entrance stays owned by ChipEntranceAnimator
                    // and the streaming assistant text is never per-delta-faded; this only smooths
                    // placement (neighbours sliding when a row mounts/grows) and removal. So a tool
                    // row appearing mid-turn slides its siblings instead of jerking the whole list.
                    modifier = transcriptItemTransition(reducedMotion, fadeEdges = false)
                        .fillMaxWidth()
                        .padding(top = if (precedingItem == null || isTurnBoundary) 0.dp else topGap),
                ) {
                    if (isTurnBoundary) {
                        HorizontalDivider(
                            color = AuraColors.outlineHairline,
                            modifier = Modifier.padding(
                                start = AuraSpacing.screenGutter,
                                top = AuraSpacing.Turn.gapAbove,
                                end = AuraSpacing.screenGutter,
                            ),
                        )
                        Spacer(modifier = Modifier.height(AuraSpacing.Turn.gapBelow))
                    }
                    ChatItemRow(
                        item = item,
                        onRetry = onRetry,
                        sessionEnded = sessionEnded,
                        overWash = overWash,
                        showActionRow = item is ChatItem.AssistantMessage && !item.isStreaming,
                        speakingKey = speakingKey,
                        isNewlyArrived = isNewlyArrived,
                        isToolGroupActive = isToolGroupActive,
                        isRunInFlight = isRunLive,
                        allowRichWidgets = allowRichWidgets,
                        onOpenWidgetInApp = onOpenWidgetInApp,
                        onNotice = onNotice,
                        onReadAloudToggle = onReadAloudToggle,
                        onUserMessageLongPress = onUserMessageLongPress,
                        onSubmitQuestionAnswer = onSubmitQuestionAnswer,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
        }

        if (!isAtBottom) {
            NewResponsesPill(
                onClick = { scope.launch { listState.animateScrollToItem(0) } },
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(bottom = AuraSpacing.Composer.gapTight),
            )
        }
    }
}

/**
 * disclaimer gate, extracted pure for unit-testability: the bottom "Mewbo is an AI tool
 * and can make mistakes." caption renders only when a reply has SETTLED ([hasSettledReply]) AND the
 * turn is no longer live ([isRunLive] false). Gating on `!isRunLive` is what stops it mounting under
 * the fresh user bubble on a follow-up turn before that turn's own response (and footer) exist.
 */
internal fun shouldShowDisclaimer(hasSettledReply: Boolean, isRunLive: Boolean): Boolean =
    hasSettledReply && !isRunLive

/**
 * the ONE shared `LazyColumn` item transition for the transcript — fades + a placement
 * spring so a live turn's mount/shuffle/unmount reads as a calm reflow instead of flicker. One spec
 * for every row rather than a bespoke transition per item.
 *
 * [fadeEdges] = the item has NO entrance animator of its own (the spark, the disclaimer) → it fades
 * on BOTH mount and unmount. `false` (content rows) leaves entrance to [ChipEntranceAnimator] (chips)
 * and never per-delta-fades the streaming assistant text, keeping only placement + a removal fade.
 * Reduced motion drops the placement travel entirely (rows snap into place) but keeps the opacity
 * fades at the flat [AuraMotion.reducedBlockFadeMs] — a fade is opacity, not motion (M8 spirit).
 */
private fun LazyItemScope.transcriptItemTransition(reducedMotion: Boolean, fadeEdges: Boolean): Modifier =
    Modifier.animateItem(
        fadeInSpec = when {
            !fadeEdges -> null
            reducedMotion -> tween(AuraMotion.reducedBlockFadeMs)
            else -> tween(AuraMotion.actionRowFadeMs)
        },
        placementSpec = if (reducedMotion) null else AuraMotion.transcriptItemPlacementSpring,
        fadeOutSpec = tween(if (reducedMotion) AuraMotion.reducedBlockFadeMs else AuraMotion.actionRowFadeMs),
    )

/** Exhaustive dispatch over [ChatItem]'s sealed union - the compiler flags a missing branch. */
@Composable
private fun ChatItemRow(
    item: ChatItem,
    onRetry: () -> Unit,
    sessionEnded: Boolean,
    overWash: Boolean,
    showActionRow: Boolean,
    speakingKey: String?,
    isNewlyArrived: Boolean,
    isToolGroupActive: Boolean,
    isRunInFlight: Boolean,
    allowRichWidgets: Boolean,
    onOpenWidgetInApp: () -> Unit,
    onNotice: (String) -> Unit,
    onReadAloudToggle: (ChatItem.AssistantMessage) -> Unit,
    onUserMessageLongPress: ((ChatItem.UserBubble) -> Unit)?,
    onSubmitQuestionAnswer: (callId: String, callToken: String, answers: List<QuestionAnswerItemDto>, onResult: (Boolean) -> Unit) -> Unit,
    modifier: Modifier = Modifier,
) {
    when (item) {
        is ChatItem.UserBubble -> UserBubbleRow(
            item = item,
            modifier = modifier,
            overWash = overWash,
            // Whether this bubble gets the gesture at all is the MODEL's rule
            // ([MessageAction.anyAvailableFor]), not an if-chain here - a bubble it offers nothing
            // for gets a null callback, so UserBubbleRow never installs the gesture and long-press
            // stays select-to-copy. `anyAvailableFor` rather than `availableFor(...).isNotEmpty()`
            // so this hot path (every user row, every recomposition) allocates no list.
            // `?.takeIf` yields either the SAME hoisted reference or null, never a fresh lambda -
            // currying here is what broke row stability once already (ui/CLAUDE.md).
            onLongPress = onUserMessageLongPress?.takeIf {
                MessageAction.anyAvailableFor(
                    pending = item.pending,
                    running = isRunInFlight,
                    sessionEnded = sessionEnded,
                    steer = item.steer,
                )
            },
        )
        is ChatItem.AssistantMessage -> AssistantMessageRow(
            item = item,
            showActionRow = showActionRow,
            isSpeaking = item.key == speakingKey,
            onNotice = onNotice,
            // Passed straight through (not curried with `item` here) - see AssistantMessageRow's
            // param doc. Currying at THIS call site allocated a fresh lambda on every itemsIndexed
            // invocation, which is exactly the per-row recomposition instability fix-round item 2
            // measures for.
            onReadAloudToggle = onReadAloudToggle,
            modifier = modifier,
        )
        is ChatItem.ToolCallGroup -> ChipEntranceAnimator(playEntrance = isNewlyArrived, modifier = modifier) {
            ToolCallGroupCard(item = item, isRunActive = isToolGroupActive)
        }
        is ChatItem.TodoList -> ChipEntranceAnimator(playEntrance = isNewlyArrived, modifier = modifier) {
            PlanCard(item = item)
        }
        // A tool promoted OUT of the collapsed fold into its own action card (ToolCardRegistry picks
        // the renderer; an unregistered id falls back to a generic card, never a blank row). The
        // card itself is static - it takes the same M7 chip entrance as the rest of the family only
        // because it arrives mid-transcript the same way they do, not because it animates.
        is ChatItem.ToolCard -> ChipEntranceAnimator(playEntrance = isNewlyArrived, modifier = modifier) {
            ToolCardRegistry.Render(call = item.call)
        }
        is ChatItem.AgentChip -> ChipEntranceAnimator(playEntrance = isNewlyArrived, modifier = modifier) {
            AgentChipRow(item = item)
        }
        // A model-built Streamlit widget: the docked chat renders it in its interactive
        // stlite WebView; the assist overlay (allowRichWidgets = false) renders the compact,
        // tap-to-open summary card instead. Same M7 chip entrance as the rest of the family - it
        // arrives mid-transcript the same way, and the WebView itself does not animate.
        is ChatItem.Widget -> ChipEntranceAnimator(playEntrance = isNewlyArrived, modifier = modifier) {
            if (allowRichWidgets) WidgetCard(widget = item) else WidgetSummaryCard(widget = item, onOpenInApp = onOpenWidgetInApp)
        }
        // A pending/settled ask-user question. Same M7 chip entrance as the rest of
        // the family — it arrives mid-transcript the same way. The card owns its own interactive state;
        // a failed submit toasts through `onNotice`, settling comes from the `user_question_answered`
        // event flipping `item.resolution`.
        is ChatItem.Question -> ChipEntranceAnimator(playEntrance = isNewlyArrived, modifier = modifier) {
            QuestionCard(item = item, sessionEnded = sessionEnded, onSubmit = onSubmitQuestionAnswer, onNotice = onNotice)
        }
        // Retry is dropped once the session is permanently terminated: a terminated session
        // 410s every mutation, so a Retry button would loop forever. ChatViewModel flips
        // `sessionEnded` from the 410 envelope (send) or the events endpoint (open); ChatUiState.
        is ChatItem.ErrorCard -> ErrorCard(reason = item.message, onRetry = if (sessionEnded) null else onRetry, modifier = modifier)
    }
}

/**
 * M7 (spec §7/§6.11): 250ms fade + 4dp rise the first time a chip-family item ([ChatItem.ToolCallGroup]/
 * [ChatItem.TodoList]/[ChatItem.AgentChip]) appears; `playEntrance = false` (already seen, or a
 * scroll-back-into-view remount) renders it immediately visible in its settled state, never
 * replaying the animation. M8 reduced motion drops the rise and uses the flat block-fade duration.
 */
@Composable
private fun ChipEntranceAnimator(playEntrance: Boolean, modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    var visible by remember { mutableStateOf(!playEntrance) }
    LaunchedEffect(Unit) { if (playEntrance) visible = true }

    val reducedMotion = LocalAssistantExtras.current.reducedMotion
    val density = LocalDensity.current
    val enter = if (reducedMotion) {
        fadeIn(tween(AuraMotion.reducedBlockFadeMs))
    } else {
        fadeIn(tween(AuraMotion.actionRowFadeMs)) +
            slideInVertically(tween(AuraMotion.actionRowFadeMs)) { with(density) { AuraMotion.actionRowRise.roundToPx() } }
    }
    AnimatedVisibility(visible = visible, enter = enter, modifier = modifier) {
        content()
    }
}

/** Spec §6.3's asymmetric-rendering rule doesn't touch the "New responses" pill; restyled to the
 * §6.9 toast anatomy at 60% width (spec §8.3/§6.12: "restyled to §6.9 toast anatomy at 60% width"). */
@Composable
private fun BoxScope.NewResponsesPill(onClick: () -> Unit, modifier: Modifier = Modifier) {
    Surface(
        shape = AuraShape.radiusPill,
        color = AuraColors.surfaceNotice,
        modifier = modifier
            .fillMaxWidth(NewResponsesPillWidthFraction)
            .minimumInteractiveComponentSize()
            .clickable(onClick = onClick),
    ) {
        Row(
            horizontalArrangement = Arrangement.Center,
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.Composer.internalPadding, vertical = AuraSpacing.Composer.gapTight),
        ) {
            Icon(imageVector = Icons.Filled.KeyboardArrowDown, contentDescription = null, tint = AuraColors.textSecondary)
            Text(text = "New responses", style = AuraType.listItem, color = AuraColors.textSecondary)
        }
    }
}

private const val NewResponsesPillWidthFraction = 0.6f
private const val STICK_THRESHOLD_PX = 24
