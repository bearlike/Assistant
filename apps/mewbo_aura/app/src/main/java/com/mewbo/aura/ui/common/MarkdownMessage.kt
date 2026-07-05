package com.mewbo.aura.ui.common

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.unit.dp
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mikepenz.markdown.compose.MarkdownElement
import com.mikepenz.markdown.m3.Markdown
import com.mikepenz.markdown.m3.markdownColor
import com.mikepenz.markdown.m3.markdownTypography
import com.mikepenz.markdown.model.markdownDimens
import com.mikepenz.markdown.model.markdownPadding
import com.mikepenz.markdown.model.rememberMarkdownState
import kotlinx.coroutines.delay
import org.intellij.markdown.IElementType
import org.intellij.markdown.MarkdownElementTypes
import org.intellij.markdown.MarkdownTokenTypes

/**
 * Thin wrapper over mikepenz's m3 `Markdown`, rendering BOTH streaming and finalized assistant text
 * (user directive 2026-07-04 - retires the old `WordFadeText` word-fade comet-tail that used to
 * carry the streaming case; ui/CLAUDE.md). Callers (`AssistantMessageRow` in `ui/chat`) MUST run the
 * buffer through [MarkdownBuffer.sanitize] first - that guard exists precisely for half-open
 * mid-stream markdown (an unclosed fence/bracket that would otherwise explode layout), as much as
 * for a finalize event landing after streaming already closed. Restyled per spec §6.4: links
 * `accentPrimary`, code blocks `surfaceInput` fill + [AuraShape.radiusThumb] (16dp) corners,
 * body/paragraph text at the spec's `bodyMessage` scale.
 *
 * Streaming smoothness is TWO cooperating parts, both here: (1) this renderer parses through a
 * `retainState = true` [rememberMarkdownState] so a content change never blanks to `State.Loading`
 * mid-stream (that blank-flash was the reported flicker); (2) [rememberStreamedText] (applied at the
 * `AssistantMessageRow` call site) bounds the reparse to ~20 Hz so the growing tree isn't relaid-out
 * per token. No per-word reveal animation — that `WordFadeText` path was retired by the same
 * 2026-07-04 directive; smoothness comes from removing churn, not adding motion.
 *
 * Headings match the reference capture (2026-07-03, GMS redroid; provenance on [AuraType.markdownH1]
 * and [AuraSpacing.Markdown]): the library's m3 defaults put h1..h5 on the Material
 * display/headline scale (57/45/36/28/24sp - a 3-4x jump over the 18sp body that read as
 * billboard-sized in chat), where the reference app uses a compressed 28/24/20sp ramp, renders h4+ as body
 * text distinguished by placement only, and separates blocks with an asymmetric ~2:1
 * above/below-heading rhythm.
 *
 * The custom `success` layout exists because the library has exactly ONE spacing knob - a flat
 * `padding.block` Spacer before EVERY block including the first (no heading-specific spacing
 * upstream). Emitting our own type-aware Spacers strictly BETWEEN blocks keeps the Rev F
 * ink-distance fix (item C3) intact: the first block stays flush so
 * [AuraSpacing.AssistantText.topGapAfterBubble] remains the only owner of the space above it, and
 * `padding.block` stays zeroed so the library contributes no spacing of its own.
 */
@Composable
fun MarkdownMessage(text: String, modifier: Modifier = Modifier) {
    val body = AuraType.bodyMessage.copy(color = AuraColors.textPrimary)
    // retainState = true is the streaming de-flicker: MarkdownStateImpl.updateInput resets the render
    // to a blank State.Loading on every content change UNLESS retainState is set, so the default path
    // blanked the whole reply for a frame on each SSE delta before the async reparse landed - that
    // blank-flash WAS the flicker. retainState keeps the last parsed tree visible until the new parse
    // (off the main thread, Dispatchers.Default) swaps in; content only ever grows, so the held frame
    // is always a clean prefix. immediate = true parses the FIRST frame synchronously so a freshly
    // mounted reply never shows that blank either. Reparse frequency is bounded upstream by
    // [rememberStreamedText] (AssistantMessageRow); flavour stays GFM (rememberMarkdownState's default,
    // == the prior `Markdown(content=...)` overload, so settled text renders byte-identically).
    val markdownState = rememberMarkdownState(content = text, retainState = true, immediate = true)
    Markdown(
        markdownState = markdownState,
        colors = markdownColor(
            text = AuraColors.textPrimary,
            codeBackground = AuraColors.surfaceInput,
            inlineCodeBackground = AuraColors.surfaceInput,
            dividerColor = AuraColors.outlineHairline,
            tableBackground = AuraColors.surfaceInput,
        ),
        typography = markdownTypography(
            h1 = AuraType.markdownH1.copy(color = AuraColors.textPrimary),
            h2 = AuraType.markdownH2.copy(color = AuraColors.textPrimary),
            h3 = AuraType.markdownH3.copy(color = AuraColors.textPrimary),
            h4 = body,
            h5 = body,
            h6 = body,
            text = body,
            paragraph = body,
            textLink = TextLinkStyles(style = SpanStyle(color = AuraColors.accentPrimary)),
        ),
        dimens = markdownDimens(codeBackgroundCornerSize = AuraShape.radiusThumb),
        padding = markdownPadding(block = 0.dp),
        success = { state, components, successModifier ->
            Column(successModifier) {
                var previous: IElementType? = null
                state.node.children.forEach { node ->
                    if (node.type != MarkdownTokenTypes.EOL &&
                        node.type != MarkdownTokenTypes.WHITE_SPACE
                    ) {
                        previous?.let { prev ->
                            val gap = when {
                                node.type in HeadingTypes -> AuraSpacing.Markdown.headingTopGap
                                prev in HeadingTypes -> AuraSpacing.Markdown.headingBottomGap
                                else -> AuraSpacing.AssistantText.paragraphGap
                            }
                            Spacer(Modifier.height(gap))
                        }
                        previous = node.type
                    }
                    MarkdownElement(node, components, state.content, includeSpacer = false)
                }
            }
        },
        modifier = modifier,
    )
}

/**
 * Streaming reparse throttle - the other half of the streaming-smoothness fix (see [MarkdownMessage]'s
 * `retainState` note for the blank-flash half). `TranscriptReducer.appendDelta` emits a new
 * [com.mewbo.aura.data.model.ChatItem.AssistantMessage] carrying the whole accumulated buffer on
 * every SSE token, so the streaming row otherwise reparses + relays-out the entire growing markdown
 * tree 30-80×/sec. This samples the buffer to at most one change per
 * [AuraMotion.markdownStreamReparseMs] (~20 Hz) WHILE [isStreaming] - the eye can't tell it from
 * per-token, but it cuts reparse/relayout ~4×. The moment [isStreaming] flips false the exact final
 * [text] is returned verbatim, so a settled reply is never left showing a throttled-stale frame. A
 * non-streaming (history/settled) message is a pure pass-through with zero added state - finalized
 * rendering is byte-for-byte unchanged, preserving the ONE-path rule (ui/CLAUDE.md).
 */
@Composable
fun rememberStreamedText(text: String, isStreaming: Boolean): String {
    if (!isStreaming) return text
    // rememberUpdatedState so the sampler loop reads the freshest buffer without restarting per delta.
    val latest by rememberUpdatedState(text)
    var shown by remember { mutableStateOf(text) }
    LaunchedEffect(Unit) {
        while (true) {
            if (shown != latest) shown = latest
            delay(AuraMotion.markdownStreamReparseMs.toLong())
        }
    }
    return shown
}

private val HeadingTypes: Set<IElementType> = setOf(
    MarkdownElementTypes.ATX_1,
    MarkdownElementTypes.ATX_2,
    MarkdownElementTypes.ATX_3,
    MarkdownElementTypes.ATX_4,
    MarkdownElementTypes.ATX_5,
    MarkdownElementTypes.ATX_6,
    MarkdownElementTypes.SETEXT_1,
    MarkdownElementTypes.SETEXT_2,
)
