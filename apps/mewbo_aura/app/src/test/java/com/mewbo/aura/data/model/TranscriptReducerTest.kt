package com.mewbo.aura.data.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The contract suite: any serialization/event change to the reducer must keep this green
 * (apps/mewbo_aura/CLAUDE.md "Testing"). Fixtures are real contract-shaped JSON event frames run
 * through [SessionEvent.decode], not hand-built domain objects, so a wire-shape regression shows
 * up here too.
 */
class TranscriptReducerTest {

    private val json = Json { ignoreUnknownKeys = true }

    private fun events(vararg raw: String): List<SessionEvent> = raw.map { SessionEvent.decode(json, it) }

    @Test
    fun `delta accumulation collapses into a single streaming item`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Hel","agent_id":"a1","depth":0,"step":1}}""",
                """{"type":"agent_message_delta","ts":"t2","payload":{"text":"lo ","agent_id":"a1","depth":0,"step":2}}""",
                """{"type":"agent_message_delta","ts":"t3","payload":{"text":"world","agent_id":"a1","depth":0,"step":3}}""",
            ),
        )
        assertEquals(1, items.size)
        val assistant = items.single() as ChatItem.AssistantMessage
        assertEquals("Hello world", assistant.text)
        assertTrue(assistant.isStreaming)
    }

    @Test
    fun `agent_message replaces the accumulated buffer instead of appending`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Hel","agent_id":"a1","depth":0}}""",
                """{"type":"agent_message_delta","ts":"t2","payload":{"text":"lx","agent_id":"a1","depth":0}}""",
                """{"type":"agent_message","ts":"t3","payload":{"text":"Hello (corrected)","agent_id":"a1","depth":0}}""",
            ),
        )
        assertEquals(1, items.size)
        val assistant = items.single() as ChatItem.AssistantMessage
        assertEquals("Hello (corrected)", assistant.text)
        assertTrue(assistant.isStreaming) // agent_message is authoritative for the step, not the whole turn
    }

    @Test
    fun `assistant event finalizes the streaming item`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Hel","agent_id":"a1","depth":0}}""",
                """{"type":"assistant","ts":"t2","payload":{"text":"Hello world"}}""",
            ),
        )
        assertEquals(1, items.size)
        val assistant = items.single() as ChatItem.AssistantMessage
        assertEquals("Hello world", assistant.text)
        assertFalse(assistant.isStreaming)
    }

    @Test
    fun `a new turn after finalization opens a second assistant item`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"assistant","ts":"t1","payload":{"text":"first"}}""",
                """{"type":"agent_message_delta","ts":"t2","payload":{"text":"second","agent_id":"a1","depth":0}}""",
            ),
        )
        assertEquals(2, items.size)
        assertEquals("first", (items[0] as ChatItem.AssistantMessage).text)
        assertEquals("second", (items[1] as ChatItem.AssistantMessage).text)
        assertTrue((items[1] as ChatItem.AssistantMessage).isStreaming)
    }

    @Test
    fun `sub-agent narration at depth greater than zero is dropped from the chat lane`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"child talk","agent_id":"a2","depth":1}}""",
                """{"type":"agent_message","ts":"t2","payload":{"text":"child result","agent_id":"a2","depth":1}}""",
            ),
        )
        assertTrue(items.isEmpty())
    }

    @Test
    fun `tool_result opens a one-call tool group with the right fields`() {
        val items = TranscriptReducer.reduce(
            events(
                """
                {"type":"tool_result","ts":"t1","payload":{
                    "tool_id":"shell","operation":"run","result":"ok output",
                    "success":true,"summary":"ran ls"
                }}
                """.trimIndent(),
            ),
        )
        val group = items.single() as ChatItem.ToolCallGroup
        val call = group.calls.single()
        assertEquals("shell", call.toolId)
        assertEquals("run", call.operation)
        assertEquals("ran ls", call.summary)
        assertTrue(call.success)
        assertEquals("ok output", call.detail)
    }

    @Test
    fun `tool_result without an explicit success flag derives it from the error field`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","error":"boom"}}""",
            ),
        )
        val call = (items.single() as ChatItem.ToolCallGroup).calls.single()
        assertFalse(call.success)
        assertEquals("boom", call.error)
    }

    @Test
    fun `consecutive tool_result events in one turn fold into a single group, not one item each`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}""",
                """{"type":"tool_result","ts":"t2","payload":{"tool_id":"grep","operation":"run","success":true}}""",
                """{"type":"tool_result","ts":"t3","payload":{"tool_id":"web_search","operation":"search","success":true}}""",
            ),
        )
        val group = items.single() as ChatItem.ToolCallGroup
        assertEquals(listOf("shell", "grep", "web_search"), group.calls.map { it.toolId })
    }

    @Test
    fun `a user turn closes the open tool group so the next tool_result starts a new one`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}""",
                """{"type":"user","ts":"t2","payload":{"text":"thanks, now check the weather"}}""",
                """{"type":"tool_result","ts":"t3","payload":{"tool_id":"web_search","operation":"search","success":true}}""",
            ),
        )
        assertEquals(3, items.size)
        val firstGroup = items[0] as ChatItem.ToolCallGroup
        val secondGroup = items[2] as ChatItem.ToolCallGroup
        assertEquals(listOf("shell"), firstGroup.calls.map { it.toolId })
        assertEquals(listOf("web_search"), secondGroup.calls.map { it.toolId })
        assertTrue("groups on either side of a user turn must not share a key", firstGroup.key != secondGroup.key)
    }

    @Test
    fun `a fresh assistant-text item no longer closes the open tool group - a later tool_result still folds into it, ahead of the text`() {
        // CHANGED: narration/assistant-text used to close the group
        // (closeToolGroup was called from openAssistantIfNeeded); real sessions interleave
        // tool_results with root narration WITHIN one turn (9/122 aura-android turns), so only a
        // fresh user/user_steer turn closes the group now. The turn's one group must still end up
        // BEFORE its assistant text (activity-precedes-narration).
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}""",
                """{"type":"assistant","ts":"t2","payload":{"text":"done with that"}}""",
                """{"type":"tool_result","ts":"t3","payload":{"tool_id":"web_search","operation":"search","success":true}}""",
            ),
        )
        assertEquals(2, items.size)
        val group = items[0] as ChatItem.ToolCallGroup
        assertEquals(listOf("shell", "web_search"), group.calls.map { it.toolId })
        val assistant = items[1] as ChatItem.AssistantMessage
        assertEquals("done with that", assistant.text)
    }

    @Test
    fun `a sub_agent event does not close the open tool group - later tool_results still append`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}""",
                """{"type":"sub_agent","ts":"t2","payload":{"action":"spawn","agent_id":"a1"}}""",
                """{"type":"tool_result","ts":"t3","payload":{"tool_id":"grep","operation":"run","success":true}}""",
            ),
        )
        assertEquals(2, items.size) // one group (2 calls) + the agent chip, not three separate items
        val group = items.filterIsInstance<ChatItem.ToolCallGroup>().single()
        assertEquals(listOf("shell", "grep"), group.calls.map { it.toolId })
        assertTrue(items.any { it is ChatItem.AgentChip })
    }

    @Test
    fun `a todos event does not close the open tool group - later tool_results still append`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}""",
                """{"type":"todos","ts":"t2","payload":{"items":[{"label":"a","status":"pending"}]}}""",
                """{"type":"tool_result","ts":"t3","payload":{"tool_id":"grep","operation":"run","success":true}}""",
            ),
        )
        val group = items.filterIsInstance<ChatItem.ToolCallGroup>().single()
        assertEquals(listOf("shell", "grep"), group.calls.map { it.toolId })
    }

    @Test
    fun `tool_input propagates through to the folded ToolCall's inputJson`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true,"tool_input":{"cmd":"ls","recursive":true}}}""",
            ),
        )
        val call = (items.single() as ChatItem.ToolCallGroup).calls.single()
        val input = call.inputJson as JsonObject
        assertEquals("ls", input["cmd"]!!.jsonPrimitive.content)
        assertEquals("true", input["recursive"]!!.jsonPrimitive.content)
    }

    @Test
    fun `a tool_result with no tool_input folds a null inputJson`() {
        val items = TranscriptReducer.reduce(
            events("""{"type":"tool_result","ts":"t1","payload":{"tool_id":"update_todos","operation":"write","success":true}}"""),
        )
        val call = (items.single() as ChatItem.ToolCallGroup).calls.single()
        assertEquals(null, call.inputJson)
    }

    @Test
    fun `the group's own key stays stable as later calls append to it`() {
        var state = TranscriptReducer.State()
        state = TranscriptReducer.fold(state, SessionEvent.decode(json, """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}"""))
        val keyAfterFirstCall = (state.chatItems.single() as ChatItem.ToolCallGroup).key

        state = TranscriptReducer.fold(state, SessionEvent.decode(json, """{"type":"tool_result","ts":"t2","payload":{"tool_id":"grep","operation":"run","success":true}}"""))
        val group = state.chatItems.single() as ChatItem.ToolCallGroup
        assertEquals(keyAfterFirstCall, group.key) // same LazyColumn item identity across the append
        assertEquals(2, group.calls.size)
        // per-call keys stay distinct so each row's own detail-expansion state is independent
        assertTrue(group.calls[0].key != group.calls[1].key)
    }

    @Test
    fun `completion with an error surfaces an error card after closing the streaming item`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"partial","agent_id":"a1","depth":0}}""",
                """{"type":"completion","ts":"t2","payload":{"done":true,"done_reason":"error","error":"boom"}}""",
            ),
        )
        assertEquals(2, items.size)
        val assistant = items[0] as ChatItem.AssistantMessage
        assertFalse(assistant.isStreaming)
        val errorCard = items[1] as ChatItem.ErrorCard
        assertEquals("boom", errorCard.message)
    }

    @Test
    fun `a successful completion carrying only last_error produces no error card`() {
        // Regression test: a run that COMPLETES SUCCESSFULLY but had one failed MCP tool call
        // along the way still carries `last_error` (real aura-android production evidence) - only
        // the completion's OWN `error` field means the run itself failed; `last_error` alone must
        // never spawn a card under an otherwise-successful chat.
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"assistant","ts":"t1","payload":{"text":"all done"}}""",
                """{"type":"completion","ts":"t2","payload":{"done":true,"done_reason":"completed","last_error":"mcp tool failed once, recovered"}}""",
            ),
        )
        assertEquals(1, items.size)
        assertTrue(items.single() is ChatItem.AssistantMessage)
    }

    @Test
    fun `a failed completion still surfaces an error card, preferring error over last_error`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"assistant","ts":"t1","payload":{"text":"partial work"}}""",
                """{"type":"completion","ts":"t2","payload":{"done":true,"done_reason":"error","error":"boom","last_error":"boom"}}""",
            ),
        )
        assertEquals(2, items.size)
        val errorCard = items[1] as ChatItem.ErrorCard
        assertEquals("boom", errorCard.message)
    }

    @Test
    fun `the real interleaved-turn shape folds into one tool group before the assistant item`() {
        // Real production shape (Mongo, aura-android session e7e398fe5ef14d67bcdeb2553322365f run
        // r3, 9/122 turns overall): tool_result -> narration deltas -> more tool_results -> more
        // narration -> assistant -> completion. Must fold into exactly ONE ToolCallGroup with all
        // 3 calls, positioned BEFORE the assistant item; the assistant item is last.
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"alpha","operation":"run","success":true}}""",
                """{"type":"agent_message_delta","ts":"t2","payload":{"text":"Hel","agent_id":"a1","depth":0}}""",
                """{"type":"agent_message_delta","ts":"t3","payload":{"text":"lo","agent_id":"a1","depth":0}}""",
                """{"type":"agent_message","ts":"t4","payload":{"text":"Hello there","agent_id":"a1","depth":0}}""",
                """{"type":"tool_result","ts":"t5","payload":{"tool_id":"beta","operation":"run","success":false,"error":"transient"}}""",
                """{"type":"tool_result","ts":"t6","payload":{"tool_id":"gamma","operation":"run","success":true}}""",
                """{"type":"agent_message_delta","ts":"t7","payload":{"text":" more","agent_id":"a1","depth":0}}""",
                """{"type":"assistant","ts":"t8","payload":{"text":"final answer"}}""",
                """{"type":"completion","ts":"t9","payload":{"done":true,"done_reason":"completed"}}""",
            ),
        )
        assertEquals(2, items.size)
        val group = items[0] as ChatItem.ToolCallGroup
        assertEquals(listOf("alpha", "beta", "gamma"), group.calls.map { it.toolId })
        val assistant = items[1] as ChatItem.AssistantMessage
        assertEquals("final answer", assistant.text)
        assertFalse(assistant.isStreaming)
    }

    @Test
    fun `narration first then tool_results still ends up before the assistant item - exercises the insert path`() {
        // A turn that starts with narration deltas (opening the assistant-text item first) before
        // any tool_result arrives - the group must be INSERTED ahead of the already-open assistant
        // item, not appended after it.
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Thinking","agent_id":"a1","depth":0}}""",
                """{"type":"tool_result","ts":"t2","payload":{"tool_id":"shell","operation":"run","success":true}}""",
            ),
        )
        assertEquals(2, items.size)
        assertTrue(items[0] is ChatItem.ToolCallGroup)
        assertTrue(items[1] is ChatItem.AssistantMessage)
        val group = items[0] as ChatItem.ToolCallGroup
        assertEquals(listOf("shell"), group.calls.map { it.toolId })
    }

    @Test
    fun `two turns each produce their own tool group, positioned before their own turn's text`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"user","ts":"t1","payload":{"text":"turn one"}}""",
                """{"type":"tool_result","ts":"t2","payload":{"tool_id":"alpha","operation":"run","success":true}}""",
                """{"type":"assistant","ts":"t3","payload":{"text":"resp one"}}""",
                """{"type":"user","ts":"t4","payload":{"text":"turn two"}}""",
                """{"type":"tool_result","ts":"t5","payload":{"tool_id":"beta","operation":"run","success":true}}""",
                """{"type":"assistant","ts":"t6","payload":{"text":"resp two"}}""",
            ),
        )
        assertEquals(6, items.size)
        val firstGroup = items[1] as ChatItem.ToolCallGroup
        val firstAssistant = items[2] as ChatItem.AssistantMessage
        val secondGroup = items[4] as ChatItem.ToolCallGroup
        val secondAssistant = items[5] as ChatItem.AssistantMessage
        assertEquals(listOf("alpha"), firstGroup.calls.map { it.toolId })
        assertEquals("resp one", firstAssistant.text)
        assertEquals(listOf("beta"), secondGroup.calls.map { it.toolId })
        assertEquals("resp two", secondAssistant.text)
        assertTrue("groups from different turns must not share a key", firstGroup.key != secondGroup.key)
    }

    @Test
    fun `the interleaved-turn shape is idempotent under a duplicated replay of the whole event list`() {
        val raw = arrayOf(
            """{"type":"tool_result","ts":"t1","payload":{"tool_id":"alpha","operation":"run","success":true}}""",
            """{"type":"agent_message_delta","ts":"t2","payload":{"text":"Hel","agent_id":"a1","depth":0}}""",
            """{"type":"tool_result","ts":"t3","payload":{"tool_id":"beta","operation":"run","success":false,"error":"transient"}}""",
            """{"type":"agent_message_delta","ts":"t4","payload":{"text":"lo","agent_id":"a1","depth":0}}""",
            """{"type":"assistant","ts":"t5","payload":{"text":"final answer"}}""",
            """{"type":"completion","ts":"t6","payload":{"done":true,"done_reason":"completed"}}""",
        )
        val once = TranscriptReducer.reduce(events(*raw))
        val replayed = TranscriptReducer.reduce(events(*raw, *raw))
        assertEquals(once, replayed)
    }

    @Test
    fun `todos replaces the whole checklist card in place, never merges`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"todos","ts":"t1","payload":{"items":[{"label":"a","status":"pending"}]}}""",
                """{"type":"assistant","ts":"t2","payload":{"text":"hi"}}""",
                """{"type":"todos","ts":"t3","payload":{"items":[{"label":"a","status":"completed"},{"label":"b","status":"pending"}]}}""",
            ),
        )
        assertEquals(2, items.size) // todos stays at its first position, not appended again
        val todos = items[0] as ChatItem.TodoList
        assertEquals(2, todos.items.size)
        assertEquals("completed", todos.items[0].status)
    }

    @Test
    fun `optimistic local echo dedupes against the server user event by text and ts window`() {
        val items = TranscriptReducer.reduce(
            events(
                // client-synthesized optimistic bubble, sent immediately on submit
                """{"type":"user","ts":"2026-06-15T18:24:00.000000+00:00","payload":{"text":"hi there"}}""",
                // server echo of the same turn, arrives a moment later
                """{"type":"user","ts":"2026-06-15T18:24:02.500000+00:00","payload":{"text":"hi there"}}""",
            ),
        )
        assertEquals(1, items.size)
        assertTrue(items.single() is ChatItem.UserBubble)
    }

    @Test
    fun `optimistic echo dedupe also matches against user_steer`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"user","ts":"2026-06-15T18:24:00.000000+00:00","payload":{"text":"steer"}}""",
                """{"type":"user_steer","ts":"2026-06-15T18:24:01.000000+00:00","payload":{"text":"steer"}}""",
            ),
        )
        assertEquals(1, items.size)
    }

    // ---- `steer` + the server-ts anchor: what makes a bubble usable as a `from_ts` ----

    @Test
    fun `a user_steer event folds to a bubble flagged steer, a plain user event does not`() {
        // The flag exists for exactly one consumer: MessageAction's gate withholds "Retry from here"
        // on a steered bubble, because /recover's from_ts scan (session_runtime.py:996) matches
        // `type == "user"` and can therefore NEVER find a `user_steer` event -> 400.
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"user","ts":"2026-06-15T18:24:00.000000+00:00","payload":{"text":"fresh turn"}}""",
                """{"type":"user_steer","ts":"2026-06-15T18:24:30.000000+00:00","payload":{"text":"steered in"}}""",
            ),
        )
        val bubbles = items.filterIsInstance<ChatItem.UserBubble>()
        assertEquals(2, bubbles.size)
        assertFalse("a plain `user` event is not a steer", bubbles[0].steer)
        assertTrue("a `user_steer` event IS a steer", bubbles[1].steer)
    }

    @Test
    fun `a steering send's optimistic echo adopts steer when the server's user_steer reconciles it`() {
        // The client folds its own echo as a SessionEvent.User (ChatViewModel.send), so the bubble is
        // BORN steer=false however it was actually sent. The server's `user_steer` arrives down the
        // dedupe branch and is the first authoritative word - if that branch dropped it, every steered
        // bubble would keep claiming it can anchor a retry.
        var state = TranscriptReducer.State()
        state = TranscriptReducer.fold(
            state,
            events("""{"type":"user","ts":"2026-07-02T03:34:41.000000Z","payload":{"text":"steer me"}}""").single(),
            pending = true,
        )
        assertFalse((state.chatItems.single() as ChatItem.UserBubble).steer)

        state = TranscriptReducer.fold(
            state,
            events("""{"type":"user_steer","ts":"2026-07-02T03:34:42.633140+00:00","payload":{"text":"steer me"}}""").single(),
        )
        val bubble = state.chatItems.single() as ChatItem.UserBubble
        assertTrue("the server's user_steer must flip the reconciled bubble to steer", bubble.steer)
        assertFalse("and settle it", bubble.pending)
    }

    @Test
    fun `a reconciled bubble adopts the SERVER ts - a client clock stamp can anchor nothing`() {
        // Regression: the optimistic echo is stamped `Instant.now()` (a LOCAL clock), which exists
        // nowhere in the server's transcript. The dedupe branch used to leave that ts in place, so
        // every message sent in the current session carried an anchor that /recover would 400 on
        // ("no user event at ts=…") and /fork would cut the transcript at a meaningless point. This
        // covers the ORDINARY fresh-turn send (pending=false) - the case that used to skip
        // reconciliation entirely.
        val clientTs = "2026-07-02T03:34:41.000000Z"
        val serverTs = "2026-07-02T03:34:42.633140+00:00"
        var state = TranscriptReducer.State()
        state = TranscriptReducer.fold(
            state,
            events("""{"type":"user","ts":"$clientTs","payload":{"text":"hi"}}""").single(),
            pending = false,
        )
        val optimistic = state.chatItems.single() as ChatItem.UserBubble
        assertEquals(clientTs, optimistic.ts)

        state = TranscriptReducer.fold(
            state,
            events("""{"type":"user","ts":"$serverTs","payload":{"text":"hi"}}""").single(),
        )
        val reconciled = state.chatItems.single() as ChatItem.UserBubble
        assertEquals("the bubble must anchor on the server's ts", serverTs, reconciled.ts)
        // The KEY must NOT follow the ts - it is the row's LazyColumn identity, and changing it would
        // re-animate a settled row. Key = identity; ts = the server anchor.
        assertEquals("the row's identity must survive the swap", optimistic.key, reconciled.key)
    }

    @Test
    fun `optimistic echo dedupe matches a client-clock Z timestamp against the real backend offset format`() {
        // Regression test for the task-B review round 3 live-run bug: the client's own optimistic
        // bubble is stamped with `Instant.now().toString()` (bare-Z form), while the real backend
        // emits an explicit offset with microseconds - `2026-07-02T03:34:42.633140+00:00` verbatim,
        // taken from the live overlay repro. Both must parse and land within the echo window.
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"user","ts":"2026-07-02T03:34:41.000000Z","payload":{"text":"what's the weather"}}""",
                """{"type":"user","ts":"2026-07-02T03:34:42.633140+00:00","payload":{"text":"what's the weather"}}""",
            ),
        )
        assertEquals(1, items.size)
        assertTrue(items.single() is ChatItem.UserBubble)
    }

    @Test
    fun `optimistic echo dedupe with the real backend offset format also matches user_steer`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"user","ts":"2026-07-02T03:34:41.000000Z","payload":{"text":"steer now"}}""",
                """{"type":"user_steer","ts":"2026-07-02T03:34:42.633140+00:00","payload":{"text":"steer now"}}""",
            ),
        )
        assertEquals(1, items.size)
    }

    @Test
    fun `an unparseable ts never crashes the reducer and both bubbles are kept`() {
        // Documented fallback: if either side of the comparison can't be parsed, withinEchoWindow
        // returns false (treated as "not the same turn") rather than throwing.
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"user","ts":"not-a-timestamp","payload":{"text":"hi"}}""",
                """{"type":"user","ts":"2026-07-02T03:34:42.633140+00:00","payload":{"text":"hi"}}""",
            ),
        )
        assertEquals(2, items.size)
    }

    @Test
    fun `two distinct user turns with identical text outside the echo window both render`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"user","ts":"2026-06-15T18:00:00.000000+00:00","payload":{"text":"yes"}}""",
                """{"type":"user","ts":"2026-06-15T18:05:00.000000+00:00","payload":{"text":"yes"}}""",
            ),
        )
        assertEquals(2, items.size)
    }

    @Test
    fun `unknown and forward-compat types are dropped silently`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"context","ts":"t1","payload":{"cwd":"/tmp"}}""",
                """{"type":"run_accepted","ts":"t2","payload":{"session_id":"s1","run_id":"s1:r1"}}""",
                """{"type":"some_future_type","ts":"t3","payload":{}}""",
            ),
        )
        assertTrue(items.isEmpty())
    }

    @Test
    fun `idempotent under a duplicated tail from SSE reconnect replay`() {
        val raw = arrayOf(
            """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Hel","agent_id":"a1","depth":0}}""",
            """{"type":"agent_message_delta","ts":"t2","payload":{"text":"lo","agent_id":"a1","depth":0}}""",
            """{"type":"assistant","ts":"t3","payload":{"text":"Hello"}}""",
        )
        val once = TranscriptReducer.reduce(events(*raw))
        val withDuplicatedTail = TranscriptReducer.reduce(events(*raw, *raw.copyOfRange(1, raw.size)))
        assertEquals(once, withDuplicatedTail)
    }

    @Test
    fun `backfill overlap between two fetched pages produces no duplicates`() {
        val page1 = arrayOf(
            """{"type":"user","ts":"t1","payload":{"text":"hi"}}""",
            """{"type":"assistant","ts":"t2","payload":{"text":"hello"}}""",
        )
        // A subsequent `after=` fetch that (per the documented edge case) re-includes the last
        // page-1 event plus genuinely new events.
        val page2Overlapping = arrayOf(
            """{"type":"assistant","ts":"t2","payload":{"text":"hello"}}""",
            """{"type":"tool_result","ts":"t3","payload":{"tool_id":"shell","operation":"run","success":true}}""",
        )
        val merged = TranscriptReducer.reduce(events(*page1, *page2Overlapping))
        val withoutOverlap = TranscriptReducer.reduce(events(*page1, page2Overlapping[1]))
        assertEquals(withoutOverlap, merged)
        assertEquals(3, merged.size)
    }

    @Test
    fun `a same-timestamp but content-distinct event in the overlap is kept, not dropped`() {
        // Regression test for the Critical from the task-B review: two events sharing an identical
        // `ts` are a real, documented edge case (api-contract.md section 4). A strictly-greater-than
        // ts cursor would silently drop the second one; content-key dedupe (keyed on the WHOLE
        // event, ts included) must not, because their payloads differ.
        val page1 = arrayOf(
            """{"type":"user","ts":"t1","payload":{"text":"hi"}}""",
            """{"type":"tool_result","ts":"t2","payload":{"tool_id":"shell","operation":"run","success":true}}""",
        )
        // A replay/backfill that repeats page1's last event verbatim (exact duplicate - must
        // collapse) AND delivers a genuinely new, different event stamped with that SAME `ts`
        // (must NOT collapse).
        val replayedWithSameTsSibling = arrayOf(
            """{"type":"tool_result","ts":"t2","payload":{"tool_id":"shell","operation":"run","success":true}}""",
            """{"type":"tool_result","ts":"t2","payload":{"tool_id":"grep","operation":"run","success":true}}""",
        )
        val merged = TranscriptReducer.reduce(events(*page1, *replayedWithSameTsSibling))
        // user + ONE tool group (the two tool_results are consecutive, so they fold together) -
        // the exact-duplicate shell call must still collapse, while the distinct grep call
        // sharing its ts must still be kept, both inside that one group's calls list.
        assertEquals(2, merged.size)
        val group = merged[1] as ChatItem.ToolCallGroup
        val toolIds = group.calls.map { it.toolId }.toSet()
        assertEquals(setOf("shell", "grep"), toolIds)
    }

    @Test
    fun `same event list in yields the same items out`() {
        val raw = arrayOf(
            """{"type":"user","ts":"t1","payload":{"text":"hi"}}""",
            """{"type":"agent_message_delta","ts":"t2","payload":{"text":"Hel","agent_id":"a1","depth":0}}""",
            """{"type":"agent_message_delta","ts":"t3","payload":{"text":"lo","agent_id":"a1","depth":0}}""",
            """{"type":"assistant","ts":"t4","payload":{"text":"Hello"}}""",
            """{"type":"completion","ts":"t5","payload":{"done":true}}""",
            """{"type":"stream_end"}""",
        )
        val first = TranscriptReducer.reduce(events(*raw))
        val second = TranscriptReducer.reduce(events(*raw))
        assertEquals(first, second)
    }

    @Test
    fun `stream_end closes any still-open streaming item`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"partial","agent_id":"a1","depth":0}}""",
                """{"type":"stream_end"}""",
            ),
        )
        val assistant = items.single() as ChatItem.AssistantMessage
        assertFalse(assistant.isStreaming)
    }

    @Test
    fun `a queued send folds pending, and the server's real echo flips it back`() {
        var state = TranscriptReducer.fold(
            TranscriptReducer.State(),
            SessionEvent.decode(json, """{"type":"user","ts":"2026-06-15T18:24:00.000000+00:00","payload":{"text":"steer this"}}"""),
            pending = true,
        )
        val queued = state.chatItems.single() as ChatItem.UserBubble
        assertTrue(queued.pending)

        state = TranscriptReducer.fold(
            state,
            SessionEvent.decode(json, """{"type":"user_steer","ts":"2026-06-15T18:24:01.000000+00:00","payload":{"text":"steer this"}}"""),
        )
        val settled = state.chatItems.single() as ChatItem.UserBubble
        assertFalse(settled.pending)
        assertEquals(queued.key, settled.key) // same row, flipped in place - not a second bubble
    }

    @Test
    fun `a non-steering send never folds pending, even before the server echo arrives`() {
        val items = TranscriptReducer.reduce(events("""{"type":"user","ts":"t1","payload":{"text":"hi"}}"""))
        assertFalse((items.single() as ChatItem.UserBubble).pending)
    }

    @Test
    fun `sub_agent spawn creates one agent chip`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"sub_agent","ts":"t1","payload":{"action":"spawn","agent_id":"a1","agent_type":"researcher"}}""",
            ),
        )
        val chip = items.single() as ChatItem.AgentChip
        assertEquals("a1", chip.agentId)
        assertEquals("researcher", chip.agentType)
        assertEquals("spawn", chip.status) // falls back to action - no status yet
        assertFalse(chip.terminal)
        assertEquals(null, chip.success)
    }

    @Test
    fun `a later sub_agent event for the same id updates the same chip, not a second one`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"sub_agent","ts":"t1","payload":{"action":"spawn","agent_id":"a1","agent_type":"researcher"}}""",
                """{"type":"sub_agent","ts":"t2","payload":{"action":"status_update","agent_id":"a1","status":"completed","steps_completed":5}}""",
            ),
        )
        assertEquals(1, items.size)
        val chip = items.single() as ChatItem.AgentChip
        assertEquals("completed", chip.status)
        assertEquals(5, chip.stepsCompleted)
        assertTrue(chip.terminal)
        assertEquals(true, chip.success)
    }

    @Test
    fun `terminal sub_agent statuses other than completed report success false`() {
        listOf("failed", "cancelled", "rejected").forEach { status ->
            val items = TranscriptReducer.reduce(
                events("""{"type":"sub_agent","ts":"t1","payload":{"action":"status_update","agent_id":"a1","status":"$status"}}"""),
            )
            val chip = items.single() as ChatItem.AgentChip
            assertTrue("status=$status should be terminal", chip.terminal)
            assertEquals("status=$status should not be success", false, chip.success)
        }
    }

    @Test
    fun `an unrecognized sub_agent action is tolerated as a non-terminal chip, never dropped or crashed on`() {
        val items = TranscriptReducer.reduce(
            events("""{"type":"sub_agent","ts":"t1","payload":{"action":"some_future_action","agent_id":"a1"}}"""),
        )
        val chip = items.single() as ChatItem.AgentChip
        assertEquals("some_future_action", chip.status)
        assertFalse(chip.terminal)
    }

    @Test
    fun `depth greater than zero agent narration still stays dropped alongside sub-agent chips`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"sub_agent","ts":"t1","payload":{"action":"spawn","agent_id":"a2","depth":1}}""",
                """{"type":"agent_message_delta","ts":"t2","payload":{"text":"child talk","agent_id":"a2","depth":1}}""",
            ),
        )
        // The sub_agent chip itself renders (it's not narration); the depth>1 delta stays dropped.
        assertEquals(1, items.size)
        assertTrue(items.single() is ChatItem.AgentChip)
    }

    @Test
    fun `a successful device_set_alarm tool_result is promoted to its own top-level ToolCard`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"device_set_alarm","operation":"set","success":true,"summary":"7am alarm set"}}""",
            ),
        )
        val card = items.single() as ChatItem.ToolCard
        assertEquals("device_set_alarm", card.call.toolId)
        assertTrue(card.call.success)
        assertTrue("a promoted call must not also spawn a ToolCallGroup", items.none { it is ChatItem.ToolCallGroup })
    }

    @Test
    fun `a failed device_set_alarm tool_result stays in the generic ToolCallGroup fold, not a ToolCard`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"device_set_alarm","operation":"set","success":false,"error":"permission denied"}}""",
            ),
        )
        val group = items.single() as ChatItem.ToolCallGroup
        assertEquals(listOf("device_set_alarm"), group.calls.map { it.toolId })
        assertTrue("a failed promoted call must never produce a confident ToolCard", items.none { it is ChatItem.ToolCard })
    }

    @Test
    fun `an unregistered tool id still folds into ToolCallGroup exactly as before - promotion regression guard`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}""",
            ),
        )
        val group = items.single() as ChatItem.ToolCallGroup
        assertEquals(listOf("shell"), group.calls.map { it.toolId })
    }

    @Test
    fun `a promoted card renders before the turn's assistant narration`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Setting your alarm","agent_id":"a1","depth":0}}""",
                """{"type":"tool_result","ts":"t2","payload":{"tool_id":"device_set_alarm","operation":"set","success":true}}""",
                """{"type":"assistant","ts":"t3","payload":{"text":"Alarm set for 7am"}}""",
            ),
        )
        assertEquals(2, items.size)
        assertTrue(items[0] is ChatItem.ToolCard)
        val assistant = items[1] as ChatItem.AssistantMessage
        assertEquals("Alarm set for 7am", assistant.text)
    }

    @Test
    fun `re-reducing the same device_set_alarm event log twice is idempotent - no duplicate ToolCard`() {
        val raw = arrayOf(
            """{"type":"tool_result","ts":"t1","payload":{"tool_id":"device_set_alarm","operation":"set","success":true}}""",
            """{"type":"assistant","ts":"t2","payload":{"text":"done"}}""",
        )
        val once = TranscriptReducer.reduce(events(*raw))
        val replayed = TranscriptReducer.reduce(events(*raw, *raw))
        assertEquals(once, replayed)
        assertEquals(1, once.count { it is ChatItem.ToolCard })
    }

    @Test
    fun `a mixed turn of one promoted and two unregistered tool calls yields one ToolCard plus a two-call group`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}""",
                """{"type":"tool_result","ts":"t2","payload":{"tool_id":"device_set_alarm","operation":"set","success":true}}""",
                """{"type":"tool_result","ts":"t3","payload":{"tool_id":"grep","operation":"run","success":true}}""",
            ),
        )
        val card = items.filterIsInstance<ChatItem.ToolCard>().single()
        assertEquals("device_set_alarm", card.call.toolId)
        val group = items.filterIsInstance<ChatItem.ToolCallGroup>().single()
        assertEquals(listOf("shell", "grep"), group.calls.map { it.toolId })
        assertEquals(2, group.calls.size) // the promoted call must never inflate the group's "Used N tools" count
    }

    // --- widget_ready fold ---

    private val widgetFrame =
        """{"type":"widget_ready","ts":"t2","payload":{"widget_id":"w1","session_id":"s1",""" +
            """"files":{"app.py":"import streamlit as st","data.json":"{}"},"requirements":["pandas"],"summary":"A chart"}}"""

    @Test
    fun `a widget_ready folds into its own top-level Widget item carrying the bundle`() {
        val items = TranscriptReducer.reduce(events(widgetFrame))
        val widget = items.single() as ChatItem.Widget
        assertEquals("w1", widget.widgetId)
        assertEquals("import streamlit as st", widget.appPy)
        assertEquals("{}", widget.dataJson)
        assertEquals(listOf("pandas"), widget.requirements)
        assertEquals("A chart", widget.summary)
        assertEquals("widget:w1", widget.key)
    }

    @Test
    fun `a widget renders before the turn's assistant narration`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Here is a chart","agent_id":"a1","depth":0}}""",
                widgetFrame,
                """{"type":"assistant","ts":"t3","payload":{"text":"Here is a chart"}}""",
            ),
        )
        assertEquals(2, items.size)
        assertTrue(items[0] is ChatItem.Widget)
        assertTrue(items[1] is ChatItem.AssistantMessage)
    }

    @Test
    fun `re-emitting the same widget_id upserts in place - no duplicate Widget, idempotent replay`() {
        val once = TranscriptReducer.reduce(events(widgetFrame))
        val replayed = TranscriptReducer.reduce(events(widgetFrame, widgetFrame))
        assertEquals(once, replayed)
        assertEquals(1, once.count { it is ChatItem.Widget })
    }

    @Test
    fun `a widget_ready does not disturb the turn's tool-group fold`() {
        // The widget is orthogonal to the fold (like a promoted card): unregistered tool_results
        // before and after it keep folding into ONE group whose count never includes the widget.
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"tool_result","ts":"t1","payload":{"tool_id":"shell","operation":"run","success":true}}""",
                widgetFrame,
                """{"type":"tool_result","ts":"t3","payload":{"tool_id":"grep","operation":"run","success":true}}""",
            ),
        )
        assertEquals(1, items.count { it is ChatItem.Widget })
        val group = items.filterIsInstance<ChatItem.ToolCallGroup>().single()
        assertEquals(listOf("shell", "grep"), group.calls.map { it.toolId })
    }

    @Test
    fun `incremental fold matches a full reduce over the same events`() {
        val raw = arrayOf(
            """{"type":"user","ts":"t1","payload":{"text":"hi"}}""",
            """{"type":"agent_message_delta","ts":"t2","payload":{"text":"Hel","agent_id":"a1","depth":0}}""",
            """{"type":"assistant","ts":"t3","payload":{"text":"Hello"}}""",
        )
        val decoded = events(*raw)
        val viaReduce = TranscriptReducer.reduce(decoded)
        val viaFold = decoded.fold(TranscriptReducer.State()) { state, event -> TranscriptReducer.fold(state, event) }
        assertEquals(viaReduce, viaFold.chatItems)
    }

    // --- ask-user questions: user_question / user_question_answered fold ---

    private val questionFrame =
        """{"type":"user_question","ts":"t2","payload":{"call_id":"q1","call_token":"tok1","questions":[""" +
            """{"header":"Scope","question":"Which scope?","options":[{"label":"A","description":"desc a"},{"label":"B","description":null}],"multi_select":false},""" +
            """{"header":"Notes","question":"Anything else?","options":[]}]}}"""

    @Test
    fun `a user_question folds into its own pending Question item carrying the group and token`() {
        val items = TranscriptReducer.reduce(events(questionFrame))
        val question = items.single() as ChatItem.Question
        assertEquals("q1", question.callId)
        assertEquals("tok1", question.callToken)
        assertEquals("question:q1", question.key)
        assertEquals(null, question.resolution) // pending until answered
        assertEquals(2, question.questions.size)
        assertEquals("Scope", question.questions[0].header)
        assertEquals(listOf("A", "B"), question.questions[0].options.map { it.label })
        assertEquals(false, question.questions[0].multiSelect)
        assertTrue(question.questions[1].options.isEmpty()) // free-text question
    }

    @Test
    fun `a question renders before the turn's assistant narration`() {
        val items = TranscriptReducer.reduce(
            events(
                """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Before I proceed","agent_id":"a1","depth":0}}""",
                questionFrame,
                """{"type":"assistant","ts":"t3","payload":{"text":"Before I proceed"}}""",
            ),
        )
        assertEquals(2, items.size)
        assertTrue(items[0] is ChatItem.Question)
        assertTrue(items[1] is ChatItem.AssistantMessage)
    }

    @Test
    fun `user_question_answered settles the matching card with the chosen answers`() {
        val answeredFrame =
            """{"type":"user_question_answered","ts":"t3","payload":{"call_id":"q1","outcome":"answered","answered_via":"console","answers":[""" +
                """{"selected_indexes":[0],"text":null},{"selected_indexes":null,"text":"looks good"}]}}"""
        val items = TranscriptReducer.reduce(events(questionFrame, answeredFrame))
        val question = items.single() as ChatItem.Question
        val resolution = question.resolution as QuestionResolution.Answered
        assertEquals("console", resolution.answeredVia)
        assertEquals(listOf(0), resolution.answers[0].selectedIndexes)
        assertEquals("looks good", resolution.answers[1].text)
    }

    @Test
    fun `a declined-or-unknown outcome settles the card as a plain dismissal, never an error`() {
        // declined/interrupted/cancelled and any unknown future value all map to Dismissed, no residue.
        val dismissedFrame =
            """{"type":"user_question_answered","ts":"t3","payload":{"call_id":"q1","outcome":"declined"}}"""
        val items = TranscriptReducer.reduce(events(questionFrame, dismissedFrame))
        val question = items.single() as ChatItem.Question
        val resolution = question.resolution as QuestionResolution.Dismissed
        assertEquals("declined", resolution.outcome)
    }

    @Test
    fun `an answered event with no matching question is ignored, not rendered as an orphan`() {
        val answeredFrame =
            """{"type":"user_question_answered","ts":"t1","payload":{"call_id":"ghost","outcome":"answered","answers":[{"text":"x"}]}}"""
        assertTrue(TranscriptReducer.reduce(events(answeredFrame)).isEmpty())
    }

    @Test
    fun `re-emitting the same question upserts in place and never resets an already-settled card`() {
        // Backlog replay re-sends both frames; content-key dedupe means the settled card is preserved.
        val answeredFrame =
            """{"type":"user_question_answered","ts":"t3","payload":{"call_id":"q1","outcome":"answered","answers":[{"selected_indexes":[0]},{"text":"ok"}]}}"""
        val once = TranscriptReducer.reduce(events(questionFrame, answeredFrame))
        val replayed = TranscriptReducer.reduce(events(questionFrame, answeredFrame, questionFrame, answeredFrame))
        assertEquals(once, replayed)
        assertEquals(1, once.count { it is ChatItem.Question })
        assertTrue((once.single() as ChatItem.Question).resolution is QuestionResolution.Answered)
    }
}
