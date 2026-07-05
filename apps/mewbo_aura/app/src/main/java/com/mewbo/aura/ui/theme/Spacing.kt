package com.mewbo.aura.ui.theme

import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * Design-v2 shape & spacing system (spec §5) — one grouping per component row in the spec table.
 * No dp literal outside `ui/theme/` (module CLAUDE.md theme discipline): a new layout needing a
 * value this set lacks is a signal to report, not to invent one at the call site.
 */
object AuraSpacing {
    /** Left text margin on drawer/search/chat surfaces. Rev F measured: 20dp → 24dp (content
     * gutters were the one place the original estimate ran too small, not too large). */
    val screenGutter: Dp = 24.dp

    object Composer {
        val height: Dp = 64.dp
        val horizontalMargin: Dp = 16.dp

        /** Overlay-only floating pill side margin (Gitea #181 P3 item 6, measured capture) -
         * WIDER than the docked in-chat composer's own [horizontalMargin] (16dp); the two are
         * deliberately separate tokens so a future in-app change can never accidentally drag the
         * overlay pill's margin along with it, or vice versa. */
        val overlayHorizontalMargin: Dp = 38.dp

        /** Bottom inset above IME/nav. */
        val bottomInset: Dp = 12.dp
        val internalPadding: Dp = 16.dp
        val iconSize: Dp = 24.dp

        /** Trailing action circle (mic/send/stop). */
        val actionCircleSize: Dp = 44.dp

        /** Tight inter-icon gap (half of [internalPadding]) — e.g. between the C5 trailing mic
         * circle and waveform tile (spec §6.2 C5, Rev E E-3). Named so call sites stop dividing
         * [internalPadding] by hand. */
        val gapTight: Dp = internalPadding / 2

        /** C3 RMS waveform bar geometry (spec §6.2 C3: "~28 vertical bars, 2dp wide, 3dp gap"). */
        val waveformBarWidth: Dp = 2.dp
        val waveformBarGap: Dp = 3.dp
        val waveformMaxBarHeight: Dp = 28.dp
    }

    object UserBubble {
        const val maxWidthFraction: Float = 0.78f

        /** Rev F measured: 20dp → 16dp. */
        val paddingHorizontal: Dp = 16.dp

        /** Rev F measured: 14dp → 12dp. */
        val paddingVertical: Dp = 12.dp

        /** Rev F measured: 16dp → 24dp (a content-gutter value, grouped with [screenGutter] and
         * [AssistantText.gutter] in the audit — all three under-measured, not over). */
        val rightMargin: Dp = 24.dp
    }

    object AssistantText {
        /** Rev F measured: 20dp → 24dp (see [screenGutter]). */
        val gutter: Dp = 24.dp

        /**
         * Compact scale (user directive 2026-07-04): 12dp explicit gap — supersedes the Rev F
         * ink-distance audit (item C3) value of 0dp above, which relied on
         * [AuraType.bodyMessage]'s then-18sp intrinsic font-metric leading (bubble bottom to
         * first assistant-text glyph) to produce ~12dp of ink on its own, with no padding token
         * contributing anything. At the compact 16sp scale that intrinsic leading shrinks, and
         * the user explicitly wants clear bubble→reply separation ("touching by a thread" is not
         * acceptable) rather than relying on incidental leading — so the gap is now an explicit
         * 12dp token, additive with whatever intrinsic leading remains at 16sp. Do not chase
         * reference parity back down to 0dp; re-derive via uiautomator bounds (bubble bottom vs.
         * assistant-text top, ÷3.5 at 560dpi) before changing this further.
         *
         * Breathability retune, user directive 2026-07-04 round 2: 12dp → 16dp — user wants more
         * air between the user bubble and the response itself ("increase the space between the
         * chat bubble and the response").
         *
         * Breathability retune, user directive 2026-07-04 round 3: 16dp → 32dp (doubled) — user
         * wants 2× the space between a user query and its corresponding assistant response.
         */
        val topGapAfterBubble: Dp = 32.dp

        /**
         * Breathability retune, user directive 2026-07-04 round 2: 12dp → 14dp — intra-turn rhythm
         * breathes with the wider bubble→reply gap; also drives `MarkdownMessage` inter-block
         * spacing (see [Markdown] KDoc, which reuses this token for plain block gaps).
         */
        val paragraphGap: Dp = 14.dp
    }

    /**
     * Intra-message block rhythm for `MarkdownMessage` — reference-app parity capture, 2026-07-03, GMS
     * redroid @560dpi: ink gap above a heading 165–181px, below 67–102px ("headings cling to what
     * follows", ~2:1), between plain blocks ~80px. Converting ink targets to Spacer values means
     * subtracting the adjacent lines' own box-beyond-ink leading (~33px combined for
     * body-adjacent pairs at [AuraType.bodyMessage]'s 26sp box): 165-35≈130px→36dp above,
     * 95-34≈61px→16dp below. Plain inter-block gap (80-33≈47px→13dp) reuses
     * [AssistantText.paragraphGap] (12dp) — same "adjacent text blocks of one turn" concept, one
     * token. Spacers are emitted BETWEEN blocks only, never before the first — that structurally
     * preserves the Rev F first-block-flush constraint ([AssistantText.topGapAfterBubble]).
     */
    object Markdown {
        val headingTopGap: Dp = 36.dp
        val headingBottomGap: Dp = 16.dp
    }

    /**
     * Turn-boundary rhythm (compact design language, user directive 2026-07-04): every turn
     * (a user message and everything the assistant produces for it) is separated from the
     * previous one by a hairline divider (AuraColors.outlineHairline, drawn by ChatTranscript)
     * padded by these gaps — separation between turns is BORDER + space, no longer whitespace
     * alone. Intent-named, not measurement-named.
     *
     * Breathability retune, user directive 2026-07-04 round 2: gapAbove/gapBelow 16dp → 24dp each,
     * so the divider now sits in a 48dp+hairline band (24 + hairline + 24) — the user wants an
     * "ample, breathable amount of space between each and every turn" and "amplified space after
     * the footer" before the next turn begins.
     *
     * Breathability retune, user directive 2026-07-04 round 3: gapAbove/gapBelow 24dp → 48dp each
     * (doubled) — user wants double the inter-turn space ("double the space than what we have
     * right now to make them more breathable"); the divider now sits in a 96dp+hairline band
     * (48 + hairline + 48).
     */
    object Turn {
        /** Gap between the previous turn's last content and the divider. */
        val gapAbove: Dp = 48.dp
        /** Gap between the divider and the new turn's user bubble. */
        val gapBelow: Dp = 48.dp
    }

    /**
     * Rev F measured: the action row's icon geometry is 48dp touching touch-cells (zero extra
     * gap between them) — not a fixed icon size plus an explicit inter-icon gap. [cellSize]
     * replaces the old `gap` token; consumers size each icon's touch target to [cellSize] and
     * lay them out with no additional spacing (the previous `iconSize` (24dp) + `gap` (28dp)
     * math predicted a 52dp pitch but rendered at 76dp on device — a double-padding bug in
     * ui/chat, fixed there, not a token problem).
     */
    object ActionRow {
        /**
         * Compact scale (user directive 2026-07-04): 20dp glyph — supersedes the Rev F
         * "unchanged" value of 24dp; only the glyph shrinks, [cellSize] (48dp) stays fixed as the
         * a11y touch-target floor. Do not chase reference parity back up.
         */
        val iconSize: Dp = 20.dp
        val cellSize: Dp = 48.dp

        /**
         * Breathability retune, user directive 2026-07-04 round 2 ("a bit more space between when
         * the response ends and when the footer icons appear"): 8dp explicit margin — supersedes
         * round 1's 4dp, which itself superseded the Rev F ink-math 0dp (that relied on a
         * [cellSize] touch cell's own (48-24)/2 = 12dp inset around a then-24dp glyph). Glyphs are
         * 20dp ([iconSize]), so the 48dp cell's inset is (48-20)/2 = 14dp; +8dp explicit lands
         * ~22dp of ink between the action row and the response content above it. Do not chase
         * reference parity back down.
         */
        val topMargin: Dp = 8.dp

        /**
         * Same ink-vs-box correction as [topMargin]: the reference's action-row-to-disclaimer ink
         * gap measures 8dp, but the ORIGINAL 8dp token here was stacking on top of the [cellSize]
         * cell's own 12dp bottom inset (12 + 8 = 20dp measured, not 8). Zeroing this token still
         * yields ~12dp of ink (the cell inset alone), not the reference's 8dp - going lower would
         * mean either shrinking the 48dp touch target below the a11y minimum or a negative offset
         * clipping into the action row's own touch cells, neither acceptable for a ~4dp residual.
         * Accepted as the best achievable value; do not add a positive number back here.
         */
        val disclaimerGap: Dp = 0.dp
    }

    object DrawerRow {
        /** Frames govern over live-app drift (Rev F §F-3.3): stays 56dp contiguous. This is the
         * ACTION-row height (New chat / Search chats) — the primary rail affordances stay roomy. */
        val height: Dp = 56.dp
        val iconSize: Dp = 24.dp

        /** Rev F measured: 24dp → 12dp. */
        val iconToLabelGap: Dp = 12.dp
        val sectionHeaderTopPad: Dp = 28.dp

        /**
         * Recents (session-history) row height (user directive 2026-07-03, compact recents rail):
         * 44dp — deliberately DENSER than the 56dp action rows above so the history list scans
         * quickly ("not as spacious as it is right now"). The DESIGN.md §1.2 "never cramped" law
         * governs conversational TURN separation in the chat surface; the rail is a navigation
         * list, not a turn stream, so density here is a feature, not a violation. Provenance is a
         * user directive, which outranks reference/measured values (DESIGN.md §1 precedence).
         */
        val recentRowHeight: Dp = 44.dp

        /** Top gap above a date sub-divider ("Today" / "Previous 7 days" / "Older") within the
         * Recents section — subordinate to [sectionHeaderTopPad] above the "Recents" header itself,
         * so the two-tier hierarchy reads as header ▸ date group ▸ rows (user directive 2026-07-03). */
        val dateGroupTopPad: Dp = 12.dp

        /** Running-session liveness dot on a recents row — tokenized here (was an 8dp literal in
         * `AuraDrawerContent`, flagged in DESIGN.md as a missing token). Rendered TRAILING so every
         * title stays flush at the [screenGutter] left edge (the pre-2026-07-03 leading icon slot
         * indented titles ~36dp past the "Recents" header — the indentation the directive removed). */
        val runningDotSize: Dp = 8.dp

        /** Gap between a recents title and its trailing [runningDotSize] liveness dot. */
        val runningDotGap: Dp = 6.dp
    }

    val searchRowHeight: Dp = 64.dp

    /**
     * Reference-style tool-call fold group (Gitea #177 W1-B) — the collapsible card that replaced the
     * old one-pill-per-`tool_result` activity chip. [rowHeight] is the same 36dp the chip family
     * (`PlanCard`/`AgentChipRow`) already used before this token existed (previously a private,
     * un-tokenized literal — closed here, not a new size).
     */
    object ActivityGroup {
        val rowHeight: Dp = 36.dp

        /** Real gap when this group (or [AgentChipRow]/[PlanCard]) is the first item directly
         * under a user bubble. A separate token from [AssistantText.topGapAfterBubble] because a
         * solid card has no intrinsic font leading to absorb into (bare assistant text does — see
         * that token's KDoc), so this gap must be fully explicit rather than leaning on leading.
         *
         * Doubled to 32dp in lockstep with [AssistantText.topGapAfterBubble] (user directive
         * 2026-07-04 round 3, 2× bubble→response air): a turn whose response opens with a tool
         * group gets the same doubled user-query→response gap as a plain-text turn, so the
         * "space after my message" reads consistently regardless of whether tools ran. */
        val topGapAfterBubble: Dp = 32.dp

        /** Cap for an expanded tool call's "Input"/"Result" code block before its own internal
         * scroll takes over - shared by both, so neither reads as more or less prominent. */
        val detailMaxHeight: Dp = 200.dp
    }

    object Greeting {
        val sparkSize: Dp = 48.dp
        val gapBelowSpark: Dp = 20.dp
    }

    /**
     * Post-send attachment indicator row (metadata-only tiles, no thumbnails) rendered above a
     * [com.mewbo.aura.data.model.ChatItem.UserBubble] - reference-app-measured task brief (2026-07-03):
     * ~88dp square, ~20dp radius, ~12dp gap to the bubble beneath. [size] deliberately reuses
     * [AuraShape.radiusCard]'s existing 20dp radius rather than adding a fourth shape token - this
     * is the same solid rounded-square CARD family as the assist overlay's response card.
     */
    object AttachmentTile {
        val size: Dp = 88.dp

        /** Gap between the tile row and the user bubble it sits above. */
        val gapToBubble: Dp = 12.dp

        /** Gap between adjacent tiles when a send carries several attachments. */
        val interTileGap: Dp = 8.dp

        /** Padding inside the tile around its glyph/label + filename. */
        val internalPadding: Dp = 8.dp
    }

    /**
     * Toast/snackbar width (spec §5: "full-width minus 24dp"). Apply as the total horizontal
     * reduction against the container's max width (e.g. 12dp margin per side for a centered pill).
     */
    val toastWidthInset: Dp = 24.dp

    /**
     * The assist overlay's Streaming-state response card (Gitea #181 P3 item 1) - a floating card
     * above the composer pill (reference-app-parity pattern, recovered from the pre-v4 `ResponseSheet`
     * with measured geometry replacing its old full-bleed 0.75-height sheet). Values are #181's
     * on-device measured captures (GMS reference), not estimates.
     */
    object ResponseCard {
        /** Side margin - TIGHTER than the composer pill's own [Composer.overlayHorizontalMargin]
         * (38dp); the card and pill are visually distinct widths by design. */
        val sideMargin: Dp = 14.dp

        /** Gap between the card's bottom edge and the composer pill's top edge. */
        val gapToPill: Dp = 11.dp

        /** Cap on the card's total height as a fraction of the overlay's own measured height,
         * before its content ([com.mewbo.aura.ui.chat.ChatTranscript]) takes over internal
         * scrolling - same role [AuraSpacing.ActivityGroup.detailMaxHeight] plays for a
         * smaller scrollable surface, just screen-relative instead of a fixed Dp since a card this
         * large needs to scale with the device. */
        const val maxHeightFraction: Float = 0.65f

        val dragHandleWidth: Dp = 27.dp
        val dragHandleHeight: Dp = 1.5.dp

        /** Ink distance from the card's top edge to the drag handle pill. */
        val dragHandleTopOffset: Dp = 5.dp

        /** Read-aloud badge glyph size (bottom control row). */
        val speakerBadgeSize: Dp = 25.dp
    }
}
