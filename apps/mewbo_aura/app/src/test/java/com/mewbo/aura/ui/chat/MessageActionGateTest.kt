package com.mewbo.aura.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The long-press message-actions gate ([MessageAction.availableFor] / [MessageAction.anyAvailableFor])
 * — the rule deciding which of Retry from here / Branch in new chat / Fork session / Copy a user
 * bubble offers, and whether `UserBubbleRow` installs its long-press gesture at all.
 *
 * Every withheld case here mirrors a HARD refusal on the backend, so a gate that let one through
 * would ship a button whose only possible outcome is an error:
 * - `pending` — the bubble is the client's own optimistic echo; its `ts` is a CLIENT clock stamp, so
 *   `resolve_recovery_query` finds no event at it (400) and `fork_session_at` would cut the
 *   transcript at a timestamp that means nothing in it.
 * - `running` — `/recover` 409s ("Session is already running"), `/fork` 409s ("Cannot fork a running
 *   session").
 * - `sessionEnded` — a permanently terminated session 410s every mutation, forks included.
 * - `steer` — RETRY ONLY. `resolve_recovery_query` (session_runtime.py:996) matches
 *   `e.get("type") == "user"`, and a steered message persists as `user_steer`, so it is invisible to
 *   that scan → ValueError → 400. Forks are unaffected: `fork_session_at` truncates on `ts <= cutoff`
 *   and never reads the event type.
 *
 * [MessageAction.Copy] is the deliberate asymmetry: it is not a session mutation, so NOTHING
 * withholds it.
 */
class MessageActionGateTest {

    private val mutations = listOf(MessageAction.RetryFromHere, MessageAction.BranchInNewChat, MessageAction.ForkSession)

    /** Every reachable combination of the four inputs. */
    private fun allStates(): List<BooleanArray> = buildList {
        listOf(true, false).forEach { pending ->
            listOf(true, false).forEach { running ->
                listOf(true, false).forEach { ended ->
                    listOf(true, false).forEach { steer ->
                        add(booleanArrayOf(pending, running, ended, steer))
                    }
                }
            }
        }
    }

    @Test
    fun `a settled bubble on an idle live session offers all four actions`() {
        assertEquals(
            listOf(MessageAction.RetryFromHere, MessageAction.BranchInNewChat, MessageAction.ForkSession, MessageAction.Copy),
            MessageAction.availableFor(pending = false, running = false, sessionEnded = false, steer = false),
        )
    }

    // ---- steer: withholds RETRY, and ONLY retry ----

    @Test
    fun `a steered bubble cannot retry - the backend's from_ts scan only matches type user`() {
        val actions = MessageAction.availableFor(pending = false, running = false, sessionEnded = false, steer = true)
        assertFalse("retry must be withheld on a steered bubble", actions.contains(MessageAction.RetryFromHere))
    }

    @Test
    fun `a steered bubble still forks - fork_session_at cuts on ts alone, never on event type`() {
        val actions = MessageAction.availableFor(pending = false, running = false, sessionEnded = false, steer = true)
        assertEquals(listOf(MessageAction.BranchInNewChat, MessageAction.ForkSession, MessageAction.Copy), actions)
    }

    // ---- the three session-mutation blockers withhold all three mutations ----

    @Test
    fun `a pending bubble offers no mutation - its ts is a client stamp, not a server anchor`() {
        assertNoMutations(MessageAction.availableFor(pending = true, running = false, sessionEnded = false, steer = false))
    }

    @Test
    fun `no mutation while a run is in flight - the backend 409s a fork and a recover alike`() {
        assertNoMutations(MessageAction.availableFor(pending = false, running = true, sessionEnded = false, steer = false))
    }

    @Test
    fun `no mutation on a terminated session - every mutation 410s`() {
        assertNoMutations(MessageAction.availableFor(pending = false, running = false, sessionEnded = true, steer = false))
    }

    // ---- Copy: the asymmetry, stated explicitly ----

    @Test
    fun `Copy survives every blocker, including all of them at once`() {
        // Copy is what gives back the select-to-copy that the long-press gesture displaces
        // (UserBubbleRow's KDoc) — if any state could withhold it, that state would leave the user
        // with NO way to copy the message at all. It is also why a user bubble is ALWAYS actionable.
        allStates().forEach { (pending, running, ended, steer) ->
            val actions = MessageAction.availableFor(pending, running, ended, steer)
            assertTrue(
                "Copy withheld for pending=$pending running=$running ended=$ended steer=$steer",
                actions.contains(MessageAction.Copy),
            )
            assertTrue(MessageAction.anyAvailableFor(pending, running, ended, steer))
        }
    }

    @Test
    fun `anyAvailableFor agrees with availableFor in every state`() {
        // The transcript's per-row gate reads anyAvailableFor (allocation-free); the sheet reads
        // availableFor. They must never disagree about whether a bubble is actionable, or a bubble
        // gets a gesture that opens an empty sheet (or none when it has actions).
        allStates().forEach { (pending, running, ended, steer) ->
            assertEquals(
                "disagreement at pending=$pending running=$running ended=$ended steer=$steer",
                MessageAction.availableFor(pending, running, ended, steer).isNotEmpty(),
                MessageAction.anyAvailableFor(pending, running, ended, steer),
            )
        }
    }

    @Test
    fun `every action carries its own label, glyph and failure copy`() {
        // Guards the "behavior on the model" shape: the sheet renders whatever the enum says and
        // reports whatever it says on failure, so a new action can never reach the UI unlabelled or
        // fail silently.
        MessageAction.entries.forEach { action ->
            assertTrue("${action.name} has a blank label", action.label.isNotBlank())
            assertTrue("${action.name} has a blank failure message", action.failureMessage.isNotBlank())
        }
        assertEquals(MessageAction.entries.size, MessageAction.entries.map { it.label }.toSet().size)
    }

    private fun assertNoMutations(actions: List<MessageAction>) {
        mutations.forEach { assertFalse("$it must be withheld", actions.contains(it)) }
    }

    private operator fun BooleanArray.component1() = this[0]
    private operator fun BooleanArray.component2() = this[1]
    private operator fun BooleanArray.component3() = this[2]
    private operator fun BooleanArray.component4() = this[3]
}
