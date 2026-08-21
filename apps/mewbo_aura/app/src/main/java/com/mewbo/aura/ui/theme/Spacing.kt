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

        /** Overlay-only floating pill height [R4]: the invocation pill reads as a full
         * conversational surface, not a media strip — "large FAB-and-a-half" (56dp M3 FAB × 1.5).
         * Deliberately a SEPARATE token from the docked [height] (64dp), same two-token law as
         * [horizontalMargin]/[overlayHorizontalMargin]. */
        val overlayHeight: Dp = 84.dp

        val horizontalMargin: Dp = 16.dp

        /** Overlay-only floating pill side margin (measured capture) -
         * WIDER than the docked in-chat composer's own [horizontalMargin] (16dp); the two are
         * deliberately separate tokens so a future in-app change can never accidentally drag the
         * overlay pill's margin along with it, or vice versa. */
        val overlayHorizontalMargin: Dp = 38.dp

        /** Bottom inset above IME/nav. */
        val bottomInset: Dp = 12.dp
        val internalPadding: Dp = 16.dp
        val iconSize: Dp = 24.dp

        /**
         * Left inset for the pre-session scope row (`ChatSurface.ComposerScopeIndicator`) so its
         * leading glyph lines up with where the docked composer pill's rounded cap ENDS and its
         * straight top edge begins — the pill's outer [horizontalMargin] (16dp) plus the stadium
         * corner radius, which is [height]/2 (32dp) because [AuraShape.radiusPill] is a 50% stadium.
         * User directive: the row read too close to the screen edge; align its content
         * with the composer's body, not the composer's outer margin. The row's RIGHT inset reuses
         * [screenGutter] so both boundaries sit well within the screen edge (never flush).
         */
        val scopeRowStartInset: Dp = horizontalMargin + height / 2

        /** Leading-glyph size for the scope row + tool-picker scope-section headers — one step down
         * from [iconSize] so the small provenance markers sit proportionate to their `chipLabel`
         * (13sp) / `sectionHeader` (14sp) text rather than competing with it. */
        val scopeRowIconSize: Dp = 16.dp

        /** Trailing action circle (mic/send/stop). */
        val actionCircleSize: Dp = 44.dp

        /** Overlay-only trailing circle/tile size [R4]: the primary voice action must
         * dominate the pill's right side (reference-parity hierarchy: talk is primary). Docked
         * keeps [actionCircleSize] (44dp). Touch floor stays minimumInteractiveComponentSize. */
        val overlayActionCircleSize: Dp = 56.dp

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
         * Compact scale (user directive): 12dp explicit gap — supersedes the Rev F
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
         * Breathability retune, user directive round 2: 12dp → 16dp — user wants more
         * air between the user bubble and the response itself ("increase the space between the
         * chat bubble and the response").
         *
         * Breathability retune, user directive round 3: 16dp → 32dp (doubled) — user
         * wants 2× the space between a user query and its corresponding assistant response.
         */
        val topGapAfterBubble: Dp = 32.dp

        /**
         * Breathability retune, user directive round 2: 12dp → 14dp — intra-turn rhythm
         * breathes with the wider bubble→reply gap; also drives `MarkdownMessage` inter-block
         * spacing (see [Markdown] KDoc, which reuses this token for plain block gaps).
         */
        val paragraphGap: Dp = 14.dp
    }

    /**
     * Intra-message block rhythm for `MarkdownMessage` — reference-app parity capture, GMS
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
     * Turn-boundary rhythm (compact design language, user directive): every turn
     * (a user message and everything the assistant produces for it) is separated from the
     * previous one by a hairline divider (AuraColors.outlineHairline, drawn by ChatTranscript)
     * padded by these gaps — separation between turns is BORDER + space, no longer whitespace
     * alone. Intent-named, not measurement-named.
     *
     * Breathability retune, user directive round 2: gapAbove/gapBelow 16dp → 24dp each,
     * so the divider now sits in a 48dp+hairline band (24 + hairline + 24) — the user wants an
     * "ample, breathable amount of space between each and every turn" and "amplified space after
     * the footer" before the next turn begins.
     *
     * Breathability retune, user directive round 3: gapAbove/gapBelow 24dp → 48dp each
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
         * Compact scale (user directive): 20dp glyph — supersedes the Rev F
         * "unchanged" value of 24dp; only the glyph shrinks, [cellSize] (48dp) stays fixed as the
         * a11y touch-target floor. Do not chase reference parity back up.
         */
        val iconSize: Dp = 20.dp
        val cellSize: Dp = 48.dp

        /**
         * Breathability retune, user directive round 2 ("a bit more space between when
         * the response ends and when the footer icons appear"): 8dp explicit margin — supersedes
         * round 1's 4dp, which itself superseded the Rev F ink-math 0dp (that relied on a
         * [cellSize] touch cell's own (48-24)/2 = 12dp inset around a then-24dp glyph). Glyphs are
         * 20dp ([iconSize]), so the 48dp cell's inset is (48-20)/2 = 14dp; +8dp explicit lands
         * ~22dp of ink between the action row and the response content above it. Do not chase
         * reference parity back down.
         */
        val topMargin: Dp = 8.dp

        /**
         * Top gap between the disclaimer and whatever precedes it. Same ink-vs-box correction as
         * [topMargin] motivated the old value here (0dp, leaning entirely on the settled
         * `ActionRow` footer's own [cellSize] cell inset for ~12dp of ink) - but that reasoning
         * assumed the footer is ALWAYS this row's visual predecessor. It isn't: `shouldShowDisclaimer`
         * only requires that SOME reply has settled somewhere in the transcript, not that the
         * transcript's actual LAST item is one. A turn that ends on a tool call/widget/question card
         * (no trailing assistant text), or a completion whose only rendered item is a
         * `ChatItem.ErrorCard`, leaves the disclaimer's true predecessor as that chip/card - which
         * carries no cell inset of its own - so the old 0dp measured as a flush, touching 0dp gap in
         * exactly those states (user report: "extremely close to the user bubble or the response
         * text itself"). User directive: comfortable, CONSISTENT padding above the disclaimer
         * regardless of what precedes it - so this is now an explicit 8dp, the same "small real gap"
         * value as [topMargin]/[Composer.gapTight] elsewhere in this file, rather than a value tuned
         * to one specific predecessor's geometry. When the footer DOES precede, this adds to its
         * ~12dp cell inset for ~20dp of ink (symmetric with [topMargin]'s own ~22dp
         * response-to-footer ink); when it doesn't, this alone is the whole gap, and it is never
         * zero. Do not chase ink-parity with the footer case back down - the point of this token is
         * that it no longer depends on the footer being there. See [disclaimerBottomGap] for the
         * matching gap below.
         */
        val disclaimerGap: Dp = 8.dp

        /**
         * Bottom gap between the disclaimer and whatever follows it (the composer, or the card edge
         * in the overlay's bounded `ResponseCard`). There was previously NO padding on the
         * disclaimer item's own trailing edge - it leaned entirely on the transcript `LazyColumn`'s
         * shared `contentPadding` ([Composer.gapTight], 8dp), a generic inset meant for every item,
         * not tuned to this one. User directive (same round as [disclaimerGap]): comfortable padding
         * below the disclaimer as well as above, so both sides read consistently rather than one
         * being an explicit token and the other an incidental shared default. Same 8dp value as
         * [disclaimerGap] for the symmetry the directive asks for.
         */
        val disclaimerBottomGap: Dp = 8.dp
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
         * Recents (session-history) row height (user directive, compact recents rail):
         * 44dp — deliberately DENSER than the 56dp action rows above so the history list scans
         * quickly ("not as spacious as it is right now"). The DESIGN.md §1.2 "never cramped" law
         * governs conversational TURN separation in the chat surface; the rail is a navigation
         * list, not a turn stream, so density here is a feature, not a violation. Provenance is a
         * user directive, which outranks reference/measured values (DESIGN.md §1 precedence).
         */
        val recentRowHeight: Dp = 44.dp

        /** Top gap above a date sub-divider ("Today" / "Previous 7 days" / "Older") within the
         * Recents section — subordinate to [sectionHeaderTopPad] above the "Recents" header itself,
         * so the two-tier hierarchy reads as header ▸ date group ▸ rows (user directive). */
        val dateGroupTopPad: Dp = 12.dp

        /** Running-session liveness dot on a recents row — tokenized here (was an 8dp literal in
         * `AuraDrawerContent`, flagged in DESIGN.md as a missing token). Rendered TRAILING so every
         * title stays flush at the [screenGutter] left edge (the prior leading icon slot
         * indented titles ~36dp past the "Recents" header — the indentation the directive removed). */
        val runningDotSize: Dp = 8.dp

        /** Gap between a recents title and its trailing [runningDotSize] liveness dot. */
        val runningDotGap: Dp = 6.dp
    }

    /**
     * Drawer-sheet (side rail) elevation — side-rail visual-polish task. Byte-verified
     * against material3 1.4.0's `NavigationDrawer.kt`: `ModalDrawerSheet`'s `drawerTonalElevation`
     * flows into an inner `Surface(... tonalElevation = ...)` call that never sets `shadowElevation`
     * — so a stock `ModalDrawerSheet` casts NO drop shadow by default, only a tonal color blend
     * (and one tinted toward [AuraColors.accentPrimary] via this app's `ColorScheme.primary`, which
     * doesn't suit the neutral rail surface anyway). [shadowElevation] is applied via
     * `Modifier.shadow(...)` at the call site instead — the same built-in primitive `Surface`'s own
     * `shadowElevation` parameter draws with, just reached one layer down since `ModalDrawerSheet`
     * doesn't expose it. 16dp matches Material's own classic modal-drawer resting elevation.
     */
    object DrawerSheet {
        val shadowElevation: Dp = 16.dp
    }

    val searchRowHeight: Dp = 64.dp

    /**
     * The settings screen's collapsible sections. Every generic value reuses an existing token —
     * [screenGutter] for the outer margin, [Composer.internalPadding] for the inner padding,
     * [Composer.gapTight] for small gaps, [DrawerRow.iconSize]/[DrawerRow.iconToLabelGap] for the
     * heading glyph. Only the four below were genuinely uncovered.
     */
    object Settings {
        /** Air between two adjacent section cards, so each reads as its own bounded unit rather
         * than one continuous list under repeated hairlines. A step down from the chat surface's
         * [Turn] band: a settings list is a control surface, not a conversation. */
        val cardGap: Dp = 12.dp

        /**
         * Floor for a settings row. Taller than the 48dp accessibility minimum because a row here
         * carries a label AND a purpose caption; the floor governs the single-line case so a row
         * with no caption still clears the touch-target law.
         */
        val rowMinHeight: Dp = 56.dp

        /** Label → purpose caption. Tight enough that the pair reads as one control rather than
         * two stacked rows. */
        val captionGap: Dp = 2.dp

        /** Status glyph beside a status word. One step below [DrawerRow.iconSize] so the badge
         * sits proportionate to its [AuraType.caption] label instead of competing with the row
         * label above it. */
        val statusIconSize: Dp = 16.dp
    }

    /**
     * Mewbo Apps gallery card + detail health row (design spec §4D). Every generic gap/padding
     * reuses an existing token ([screenGutter], [Composer.internalPadding], [Composer.gapTight],
     * [DrawerRow.runningDotSize]/[DrawerRow.runningDotGap] for the status dot) — only the app icon
     * slot (a large emoji glyph, not a Material icon) has no existing analog.
     */
    object AppCard {
        /** The `icon` field (an emoji, spec §3) rendered at display scale in a fixed square slot so
         * every gallery row / the detail health row's title aligns regardless of the emoji glyph's
         * own metrics. */
        val iconSlot: Dp = 40.dp

        /** Gap between adjacent gallery cards — a step down from [Turn]'s 48dp chat-turn rhythm; a
         * gallery is a dense list, not a conversation. */
        val cardGap: Dp = 12.dp
    }

    /**
     * Reference-style tool-call fold group — the collapsible card that replaced the
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
         * round 3, 2× bubble→response air): a turn whose response opens with a tool
         * group gets the same doubled user-query→response gap as a plain-text turn, so the
         * "space after my message" reads consistently regardless of whether tools ran. */
        val topGapAfterBubble: Dp = 32.dp

        /** Cap for an expanded tool call's "Input"/"Result" code block before its own internal
         * scroll takes over - shared by both, so neither reads as more or less prominent. */
        val detailMaxHeight: Dp = 200.dp
    }

    /**
     * The promoted-tool "action card" (`ui/chat/toolcards/ToolActionCard`) - a filled card that
     * lifts a handful of allowlisted tools OUT of the collapsed [ActivityGroup] tool fold onto their
     * own surface (it renders ABOVE the turn's prose reply, not instead of it). Deliberately NOT a
     * member of the chip family: those are 36dp glance rows, this is a payload surface, so it
     * carries its own geometry.
     *
     * Reference-measured on a GMS device (560dpi, dp = px/3.5). The outer gutter (24dp)
     * and the horizontal inner padding (16dp) already had tokens - [AssistantText.gutter] and
     * [Composer.internalPadding] - and are reused verbatim; only the four values below were
     * genuinely uncovered.
     */
    object ToolCard {
        /** Roomier than the 16dp horizontal padding on purpose: the payload is a big display line
         * that needs air above and below it, and the asymmetry is what makes the card read as a
         * calm block rather than a cramped one. */
        val paddingVertical: Dp = 20.dp

        /** Header glyph - one step DOWN from the chip family's 18dp. The header answers "which
         * tool is this", so it must not compete with the content slot underneath it. */
        val headerIconSize: Dp = 16.dp

        /** Header glyph -> label. Tight enough that the pair reads as one unit (a label with a
         * glyph), not as two items in a row - hence smaller than [Composer.gapTight]. */
        val headerIconGap: Dp = 4.dp

        /** Header -> content slot: the one hierarchy break inside the card. */
        val headerToContentGap: Dp = 12.dp
    }

    object Greeting {
        val sparkSize: Dp = 48.dp
        val gapBelowSpark: Dp = 20.dp
    }

    /**
     * Stlite widget card. A widget is a chat attachment, not a page - it renders in a
     * bounded WebView that scrolls INTERNALLY when its content overflows, so the chat pane stays the
     * chat pane (the console's `WidgetCard` sizing note). [height] is a fixed cap rather than the
     * console's content-height-mirroring dance - a phone WebView can't hand Compose a live content
     * height without a JS resize bridge, and a stable cap avoids the card growing without bound; the
     * console's own MAX is 600px / 70vh, and 360dp sits comfortably under that on a phone.
     * [summaryPadding] is the inset for the overlay's compact summary rendering.
     */
    object Widget {
        val height: Dp = 360.dp
        val summaryPadding: Dp = 16.dp

        /**
         * The "view full screen" affordance below the bounded card. [affordanceIcon]
         * is the compact 20dp glyph scale shared with [ActionRow.iconSize] (the 48dp a11y touch
         * target is [androidx.compose.material3.IconButton]'s own intrinsic minimum, so it needs no
         * token); [affordanceGap] is the breathing room above and below it so it reads as a distinct
         * control, not part of the widget.
         */
        val affordanceIcon: Dp = 20.dp
        val affordanceGap: Dp = 8.dp
    }

    /**
     * Post-send attachment indicator row (metadata-only tiles, no thumbnails) rendered above a
     * [com.mewbo.aura.data.model.ChatItem.UserBubble] - reference-app-measured task brief:
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
     * The assist overlay's Streaming-state response card - a floating card
     * above the composer pill (reference-app-parity pattern, recovered from the pre-v4 `ResponseSheet`
     * with measured geometry replacing its old full-bleed 0.75-height sheet). Values are v5's
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

    /**
     * The D-pad focus ring's geometry. One grouping, because a ring that varies per surface stops
     * reading as "this is where you are" and starts reading as decoration.
     *
     * Sized to be legible from a couch rather than from arm's length: a television is viewed at
     * roughly three times a handset's distance, so the hairline weight used for [outlineHairline]
     * dividers disappears entirely at that range.
     */
    object Focus {
        /** Ring stroke. Thicker than a divider on purpose — see the object KDoc. */
        val ringWidth: Dp = 2.dp

        /** Corner rounding for the default ring, matching the row-shaped surfaces it most often
         * wraps. A caller whose target has its own silhouette (a circle, a pill) passes its own
         * shape instead of inheriting this. */
        val ringCornerRadius: Dp = 12.dp
    }

    /**
     * The television shell's permanent navigation rail.
     *
     * A fixed width rather than a fraction, unlike the handheld drawer's 0.78 of the screen: that
     * fraction exists because a sheet laid over content should not fully cover it, and a rail
     * covers nothing. What it must do instead is leave the transcript enough room to stay the
     * subject — at the 960dp width a 16:9 television reports, this keeps roughly three quarters of
     * the screen for the conversation while still fitting a session title without truncating it to
     * a stub.
     */
    object NavigationRail {
        val width: Dp = 240.dp
    }

}
