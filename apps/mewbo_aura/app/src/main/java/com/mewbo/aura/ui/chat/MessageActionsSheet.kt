package com.mewbo.aura.ui.chat

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.ui.common.SheetActionRow
import com.mewbo.aura.ui.common.SheetErrorCaption
import com.mewbo.aura.ui.common.SheetHeader
import com.mewbo.aura.ui.common.SheetShape
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.VectorGlyphFill

/**
 * What a long-press on a user message can do, and — the part that matters — WHEN. Each action owns
 * its own label, glyph, failure copy AND its own availability rule ([isAvailable]), so adding a
 * fifth is one entry here plus one branch in [MessageActionsSheet]'s dispatch `when` (which the
 * compiler forces you to write); there is no `if (kind == …)` chain in the composable to drift out
 * of sync with it.
 *
 * **The rule is deliberately NOT uniform across the four, because the backend's isn't.** Three of
 * these are session MUTATIONS (`/recover`, `/fork`) and are gated identically; [Copy] touches
 * nothing but the clipboard, so nothing can withhold it. And [RetryFromHere] carries one extra
 * blocker the two forks don't ([steer][ChatItem.UserBubble.steer]) — see its own doc. Encoding that
 * per-entry rather than as one shared predicate is what keeps a future action from silently
 * inheriting a gate that was never about it.
 *
 * [availableFor] / [anyAvailableFor] are the whole rule, as pure functions — [MessageActionGateTest]
 * is their contract test.
 */
enum class MessageAction(val label: String, val icon: ImageVector, val failureMessage: String) {
    /**
     * `POST /recover {action: "retry", from_ts}` — DESTRUCTIVE: the backend deletes this turn and
     * everything after it, then re-runs this same message in the SAME session.
     *
     * The one action a STEERED bubble cannot do. `SessionRuntime.resolve_recovery_query`
     * (`session_runtime.py:996`) resolves `from_ts` with `e.get("type") == "user" and e.get("ts") ==
     * from_ts`, and a steered message persists as `user_steer` — so it is invisible to that scan and
     * the retry raises `ValueError` → 400, every time. Withheld rather than offered-and-failed.
     */
    RetryFromHere("Retry from here", Icons.Filled.Refresh, "Couldn't retry from here") {
        override fun isAvailable(pending: Boolean, running: Boolean, sessionEnded: Boolean, steer: Boolean): Boolean =
            mutationAllowed(pending, running, sessionEnded) && !steer
    },

    /**
     * `POST /fork {from_ts}` — copies the transcript UP TO AND INCLUDING this message into a new
     * session. The original is untouched.
     *
     * Unaffected by [steer]: `SessionStore.fork_session_at` truncates purely on `ts <= cutoff` and
     * never inspects the event's TYPE, so a steered message's ts is a perfectly valid cut point.
     */
    BranchInNewChat("Branch in new chat", BranchGlyph, "Couldn't branch into a new chat") {
        override fun isAvailable(pending: Boolean, running: Boolean, sessionEnded: Boolean, steer: Boolean): Boolean =
            mutationAllowed(pending, running, sessionEnded)
    },

    /** `POST /fork {}` — copies the WHOLE transcript into a new session. The original is untouched.
     * Same gate as [BranchInNewChat]: it is the same call without a cut point. */
    ForkSession("Fork session", ForkGlyph, "Couldn't fork session") {
        override fun isAvailable(pending: Boolean, running: Boolean, sessionEnded: Boolean, steer: Boolean): Boolean =
            mutationAllowed(pending, running, sessionEnded)
    },

    /**
     * The message text to the clipboard. **Always available — no exceptions.** It is not a session
     * mutation, so none of the three blockers below can touch it: a pending bubble's text is already
     * on screen, a running session is irrelevant to the clipboard, and a terminated session's
     * transcript is still readable. This row is also what keeps long-press from being a net
     * accessibility LOSS: opening this sheet displaces the bubble's `SelectionContainer`
     * select-to-copy (they are the same gesture — see [UserBubbleRow]), so copy has to live here.
     */
    Copy("Copy", ChatIcons.ContentCopy, "Couldn't copy") {
        override fun isAvailable(pending: Boolean, running: Boolean, sessionEnded: Boolean, steer: Boolean): Boolean = true
    };

    /** This action's own availability rule. See the class doc for why it is per-entry and not shared. */
    abstract fun isAvailable(pending: Boolean, running: Boolean, sessionEnded: Boolean, steer: Boolean): Boolean

    companion object {
        /**
         * The shared half of the rule: whether the SESSION will accept a mutation at all. Every
         * `/recover` + `/fork` action is gated on exactly this; [Copy] is gated on none of it.
         *
         * - [pending] — the client's own optimistic echo of a not-yet-acknowledged send. Its `ts` is
         *   a CLIENT clock stamp, not a server anchor: no event exists at that timestamp, so it can
         *   anchor neither a retry (`resolve_recovery_query` 400s: "no user event at ts=…") nor a
         *   fork (`fork_session_at` would cut the transcript at a timestamp that means nothing in it,
         *   silently keeping the wrong events).
         * - [running] — the backend 409s a fork ("Cannot fork a running session") and a recover
         *   ("Session is already running") while a run is in flight.
         * - [sessionEnded] — a permanently terminated session 410s every mutation, forks and
         *   recoveries included. Offering an action that can only ever fail is the UI lying.
         */
        private fun mutationAllowed(pending: Boolean, running: Boolean, sessionEnded: Boolean): Boolean =
            !pending && !running && !sessionEnded

        /** The rows the sheet renders, in declaration order. */
        fun availableFor(pending: Boolean, running: Boolean, sessionEnded: Boolean, steer: Boolean): List<MessageAction> =
            entries.filter { it.isAvailable(pending, running, sessionEnded, steer) }

        /** Whether the bubble gets a long-press gesture AT ALL — the transcript's per-row gate. Kept
         * separate from [availableFor] purely so the hot path (every user row, every recomposition)
         * answers the question without allocating the list. */
        fun anyAvailableFor(pending: Boolean, running: Boolean, sessionEnded: Boolean, steer: Boolean): Boolean =
            entries.any { it.isAvailable(pending, running, sessionEnded, steer) }
    }
}

/**
 * Long-press actions for one user message in the transcript — the chat-side sibling of
 * `ui/navigation/SessionActionsSheet` (the drawer's Recents-row sheet), sharing its exact chrome via
 * `ui/common/ActionSheet` and its exact callback contract: each action hands back an
 * `onResult: (Boolean) -> Unit` the caller invokes once its coroutine settles, so this composable
 * owns no repository access — only the in-sheet busy/error state. On success it dismisses itself; on
 * failure it STAYS OPEN with a one-line inline caption and the action is simply retryable (no toast
 * on top of the sheet that raised it — DESIGN.md §6, no error residue).
 *
 * It renders no pane machine (unlike its sibling's Rename drill-in): three flat rows, so the sheet's
 * whole state is `busy` + `error`.
 *
 * [running]/[sessionEnded] are read LIVE rather than captured when the sheet opened, so a session
 * that terminates under an open sheet empties its rows instead of offering an action that would now
 * 410. Long-press already fired the row's one haptic ([UserBubbleRow]'s `combinedClickable`) —
 * nothing here fires a second.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MessageActionsSheet(
    message: ChatItem.UserBubble,
    running: Boolean,
    sessionEnded: Boolean,
    onDismiss: () -> Unit,
    onRetryFromHere: (onResult: (Boolean) -> Unit) -> Unit,
    onBranchInNewChat: (onResult: (Boolean) -> Unit) -> Unit,
    onForkSession: (onResult: (Boolean) -> Unit) -> Unit,
    /** Copies [message]'s text. Same `onResult` shape as the other three purely so this composable
     * needs no special case — the host settles it synchronously (`ChatScreen` writes the clipboard
     * and fires its "Copied" notice, the SAME idiom the assistant action row's Copy already uses). */
    onCopy: (onResult: (Boolean) -> Unit) -> Unit,
    modifier: Modifier = Modifier,
) {
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(),
        containerColor = AuraColors.surfaceInput,
        shape = SheetShape,
        modifier = modifier,
    ) {
        Column(modifier = Modifier.fillMaxWidth().padding(bottom = AuraSpacing.Composer.internalPadding)) {
            // The message itself is the header — which turn these actions apply to is the one thing
            // the sheet must make unambiguous, and the transcript row behind it is now covered.
            SheetHeader(text = message.text)
            HorizontalDivider(color = AuraColors.outlineHairline)
            MessageAction.availableFor(
                pending = message.pending,
                running = running,
                sessionEnded = sessionEnded,
                steer = message.steer,
            ).forEach { action ->
                SheetActionRow(
                    label = action.label,
                    icon = action.icon,
                    enabled = !busy,
                    onClick = {
                        error = null
                        busy = true
                        val onResult: (Boolean) -> Unit = { success ->
                            busy = false
                            if (success) onDismiss() else error = action.failureMessage
                        }
                        when (action) {
                            MessageAction.RetryFromHere -> onRetryFromHere(onResult)
                            MessageAction.BranchInNewChat -> onBranchInNewChat(onResult)
                            MessageAction.ForkSession -> onForkSession(onResult)
                            MessageAction.Copy -> onCopy(onResult)
                        }
                    },
                )
            }
            error?.let { SheetErrorCaption(it) }
        }
    }
}

/**
 * Hand-ported "branch" glyph — `material-icons-core` (this app's only icon dependency, no
 * `material-icons-extended`) ships no branch/fork glyph, and `Icons.Filled.Share` — whose artwork IS
 * a node topology — would read as "Share" to the next person editing this file. Same convention as
 * `SessionActionsSheet`'s `ArchiveGlyph` and `ChatIcons`' glyphs: a simplified stroked house-style
 * shape (a stem rising and splitting into two arms, each ending in a node), recognizable as
 * "branch", not a literal Material Symbols path trace.
 */
private val BranchGlyph: ImageVector by lazy {
    ImageVector.Builder(name = "Branch", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
        .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
            // Stem up from the base, then splitting into two arms.
            moveTo(12f, 21f)
            verticalLineTo(14f)
            moveTo(12f, 14f)
            lineTo(6.5f, 8.5f)
            moveTo(12f, 14f)
            lineTo(17.5f, 8.5f)
        }
        .path(fill = SolidColor(VectorGlyphFill)) {
            // The two arm tips, as solid nodes.
            moveTo(3.5f, 3.5f)
            horizontalLineToRelative(4f)
            verticalLineToRelative(4f)
            horizontalLineToRelative(-4f)
            close()
            moveTo(16.5f, 3.5f)
            horizontalLineToRelative(4f)
            verticalLineToRelative(4f)
            horizontalLineToRelative(-4f)
            close()
        }
        .build()
}

/**
 * Hand-ported "fork the whole session" glyph, same convention as [BranchGlyph] above. It must read
 * as clearly DIFFERENT from two neighbours at once: from [BranchGlyph] (which splits partway up, at
 * the message) and from `ChatIcons.ContentCopy`, which now belongs to [MessageAction.Copy] and can no
 * longer double as "duplicate the session". So this one diverges at the BASE and runs a second lane
 * the whole way up alongside the trunk — the entire conversation copied, not a split partway through.
 */
private val ForkGlyph: ImageVector by lazy {
    ImageVector.Builder(name = "ForkSession", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
        .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
            // The original lane, full height.
            moveTo(7f, 6f)
            verticalLineTo(20f)
            // A second lane peeling off the BASE and rising the whole way alongside it.
            moveTo(7f, 17f)
            curveTo(13f, 17f, 17f, 15f, 17f, 10f)
            verticalLineTo(6f)
        }
        .path(fill = SolidColor(VectorGlyphFill)) {
            // A node at the head of each lane.
            moveTo(5f, 2f)
            horizontalLineToRelative(4f)
            verticalLineToRelative(4f)
            horizontalLineToRelative(-4f)
            close()
            moveTo(15f, 2f)
            horizontalLineToRelative(4f)
            verticalLineToRelative(4f)
            horizontalLineToRelative(-4f)
            close()
        }
        .build()
}
