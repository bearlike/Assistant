package com.mewbo.aura.data.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Every known `type` round-trips from a contract-shaped JSON string (api-contract.md), and any
 * unrecognized/malformed frame degrades to [SessionEvent.Unknown] rather than throwing - this is
 * what lets brand-new backend event types ship without crashing the app.
 */
class SessionEventTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `user event round-trips`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"user","ts":"2026-06-15T18:24:05.412903+00:00","payload":{"text":"hello"}}""",
        )
        val user = event as SessionEvent.User
        assertEquals("hello", user.payload.text)
        assertEquals("2026-06-15T18:24:05.412903+00:00", user.ts)
    }

    @Test
    fun `user_steer event round-trips as a distinct type from user`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"user_steer","ts":"2026-06-15T18:24:06.000000+00:00","payload":{"text":"steer me"}}""",
        )
        val steer = event as SessionEvent.UserSteer
        assertEquals("steer me", steer.payload.text)
    }

    @Test
    fun `agent_message_delta round-trips with depth and step`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"agent_message_delta","ts":"t1","payload":{"text":"Hel","agent_id":"a1","depth":0,"step":3}}""",
        )
        val delta = event as SessionEvent.AgentMessageDelta
        assertEquals("Hel", delta.payload.text)
        assertEquals("a1", delta.payload.agentId)
        assertEquals(0, delta.payload.depth)
        assertEquals(3, delta.payload.step)
    }

    @Test
    fun `agent_message round-trips`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"agent_message","ts":"t1","payload":{"text":"Hello there","agent_id":"a1","depth":0}}""",
        )
        val message = event as SessionEvent.AgentMessage
        assertEquals("Hello there", message.payload.text)
    }

    @Test
    fun `assistant round-trips`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"assistant","ts":"t2","payload":{"text":"Final reply"}}""",
        )
        val assistant = event as SessionEvent.Assistant
        assertEquals("Final reply", assistant.payload.text)
    }

    @Test
    fun `tool_result round-trips including undocumented-but-always-emitted fields`() {
        val event = SessionEvent.decode(
            json,
            """
            {"type":"tool_result","ts":"t3","payload":{
                "tool_id":"shell","operation":"run","tool_input":{"cmd":"ls"},
                "result":"file1\nfile2","success":true,"summary":"ok","result_file":null,
                "agent_id":"a1","model":"gpt-x"
            }}
            """.trimIndent(),
        )
        val toolResult = event as SessionEvent.ToolResult
        assertEquals("shell", toolResult.payload.toolId)
        assertEquals("run", toolResult.payload.operation)
        assertEquals(true, toolResult.payload.success)
        assertEquals("ok", toolResult.payload.summary)
        assertEquals("a1", toolResult.payload.agentId)
        assertEquals("gpt-x", toolResult.payload.model)
    }

    @Test
    fun `completion round-trips with error fields`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"completion","ts":"t4","payload":{"done":true,"done_reason":"error","error":"boom","last_error":"boom"}}""",
        )
        val completion = event as SessionEvent.Completion
        assertTrue(completion.payload.done)
        assertEquals("error", completion.payload.doneReason)
        assertEquals("boom", completion.payload.error)
    }

    @Test
    fun `todos round-trips its item list`() {
        val event = SessionEvent.decode(
            json,
            """
            {"type":"todos","ts":"t5","payload":{
                "items":[{"label":"do thing","status":"in_progress"}],
                "source":"agent","agent_id":"a1"
            }}
            """.trimIndent(),
        )
        val todos = event as SessionEvent.Todos
        assertEquals(1, todos.payload.items.size)
        assertEquals("do thing", todos.payload.items[0].label)
        assertEquals("in_progress", todos.payload.items[0].status)
    }

    @Test
    fun `device_tool_call round-trips its full payload`() {
        val event = SessionEvent.decode(
            json,
            """
            {"type":"device_tool_call","ts":"t6","payload":{
                "call_id":"call-1","call_token":"token-1","tool_id":"device_get_time",
                "args":{},"expires_at":1799999999.5
            }}
            """.trimIndent(),
        )
        val call = event as SessionEvent.DeviceToolCall
        assertEquals("call-1", call.payload.callId)
        assertEquals("token-1", call.payload.callToken)
        assertEquals("device_get_time", call.payload.toolId)
        assertEquals(1799999999.5, call.payload.expiresAt, 0.0)
    }

    @Test
    fun `device_tool_call carries its args object verbatim`() {
        val event = SessionEvent.decode(
            json,
            """
            {"type":"device_tool_call","ts":"t7","payload":{
                "call_id":"call-2","call_token":"token-2","tool_id":"device_set_alarm",
                "args":{"hour":7,"minute":30},"expires_at":1799999999.0
            }}
            """.trimIndent(),
        )
        val call = event as SessionEvent.DeviceToolCall
        assertEquals(7, call.payload.args["hour"]?.jsonPrimitive?.int)
        assertEquals(30, call.payload.args["minute"]?.jsonPrimitive?.int)
    }

    @Test
    fun `user_question round-trips its question group and single-use token`() {
        val event = SessionEvent.decode(
            json,
            """
            {"type":"user_question","ts":"2026-07-17T00:00:00.000000+00:00","payload":{
                "call_id":"q-1","call_token":"tok-1","questions":[
                    {"header":"Auth method","question":"Which auth?","options":[
                        {"label":"OAuth","description":"Recommended"},{"label":"API key","description":null}
                    ],"multi_select":false},
                    {"header":"Notes","question":"Anything else?","options":[]}
                ]
            }}
            """.trimIndent(),
        )
        val q = event as SessionEvent.UserQuestion
        assertEquals("q-1", q.payload.callId)
        assertEquals("tok-1", q.payload.callToken)
        assertEquals(2, q.payload.questions.size)
        assertEquals("Auth method", q.payload.questions[0].header)
        assertEquals("OAuth", q.payload.questions[0].options[0].label)
        assertEquals("Recommended", q.payload.questions[0].options[0].description)
        assertEquals(false, q.payload.questions[0].multiSelect)
        // The second question has an empty options list ⇒ a free-text question (options default []).
        assertTrue(q.payload.questions[1].options.isEmpty())
    }

    @Test
    fun `user_question_answered round-trips answers and answered_via`() {
        val event = SessionEvent.decode(
            json,
            """
            {"type":"user_question_answered","ts":"2026-07-17T00:00:01.000000+00:00","payload":{
                "call_id":"q-1","outcome":"answered","answered_via":"console","answers":[
                    {"selected_indexes":[0],"text":null},{"selected_indexes":null,"text":"looks good"}
                ]
            }}
            """.trimIndent(),
        )
        val answered = event as SessionEvent.UserQuestionAnswered
        assertEquals("q-1", answered.payload.callId)
        assertEquals("answered", answered.payload.outcome)
        assertEquals("console", answered.payload.answeredVia)
        val answers = answered.payload.answers!!
        assertEquals(listOf(0), answers[0].selectedIndexes)
        assertEquals("looks good", answers[1].text)
    }

    @Test
    fun `user_question_answered tolerates a bare dismissal outcome with no answers`() {
        // declined/interrupted/cancelled carry no answers; a new unknown outcome must decode too.
        val event = SessionEvent.decode(
            json,
            """{"type":"user_question_answered","ts":"t","payload":{"call_id":"q-2","outcome":"interrupted"}}""",
        )
        val answered = event as SessionEvent.UserQuestionAnswered
        assertEquals("interrupted", answered.payload.outcome)
        assertEquals(null, answered.payload.answers)
        assertEquals(null, answered.payload.answeredVia)
    }

    @Test
    fun `stream_end has no ts on the wire and still decodes`() {
        val event = SessionEvent.decode(json, """{"type":"stream_end"}""")
        assertTrue(event is SessionEvent.StreamEnd)
        assertEquals("", event.ts)
    }

    @Test
    fun `unknown type never throws and preserves the raw frame`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"some_future_type","ts":"t9","payload":{"whatever":"shape"}}""",
        )
        val unknown = event as SessionEvent.Unknown
        assertEquals("some_future_type", unknown.type)
        assertEquals("t9", unknown.ts)
    }

    @Test
    fun `there is no dedicated error type - it falls through to Unknown, matching the verified contract`() {
        // api-contract.md section 6: the session transcript never emits `type == "error"`; a client
        // that assumed it existed must still not crash if it somehow appeared.
        val event = SessionEvent.decode(json, """{"type":"error","ts":"t10","payload":{"message":"boom"}}""")
        assertTrue(event is SessionEvent.Unknown)
    }

    @Test
    fun `known type with malformed payload degrades to Unknown instead of throwing`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"tool_result","ts":"t11","payload":"not-an-object"}""",
        )
        assertTrue(event is SessionEvent.Unknown)
    }

    @Test
    fun `garbage input never throws`() {
        val event = SessionEvent.decode(json, "not json at all")
        assertTrue(event is SessionEvent.Unknown)
    }

    // --- lastContextModel (ChatViewModel.bind's session-model hydration, review fix) ---

    @Test
    fun `lastContextModel reads model off a context event, which decodes as Unknown`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"client":"aura-android","model":"claude-sonnet-5"}}"""),
        )
        assertEquals("claude-sonnet-5", SessionEvent.lastContextModel(events))
    }

    @Test
    fun `lastContextModel picks the MOST RECENT context event, never merges across several`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"model":"claude-haiku-4-5"}}"""),
            SessionEvent.decode(json, """{"type":"user","ts":"t2","payload":{"text":"hi"}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t3","payload":{"model":"claude-sonnet-5"}}"""),
        )
        assertEquals("claude-sonnet-5", SessionEvent.lastContextModel(events))
    }

    @Test
    fun `lastContextModel is null when the session has no context event at all`() {
        val events = listOf(SessionEvent.decode(json, """{"type":"user","ts":"t1","payload":{"text":"hi"}}"""))
        assertEquals(null, SessionEvent.lastContextModel(events))
    }

    @Test
    fun `lastContextModel is null when the most recent context event has no model field`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"model":"claude-sonnet-5"}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t2","payload":{"project":"Assistant"}}"""),
        )
        // Mirrors backend.py _load_last_context semantics: the latest context event wins VERBATIM,
        // never merged with an earlier one - a model set two turns ago does not leak forward.
        assertEquals(null, SessionEvent.lastContextModel(events))
    }

    @Test
    fun `lastContextModel treats a blank model field the same as absent`() {
        val events = listOf(SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"model":""}}"""))
        assertEquals(null, SessionEvent.lastContextModel(events))
    }

    // --- lastContextProject (ChatViewModel.bind's session-project hydration, project-reset bug) ---
    // Same _load_last_context semantics as the model above: the persisted `context` event carries
    // `project` verbatim (backend _build_context_payload copies the whole context object), so a
    // revisited session's frozen scope can be restored to the project its NEXT turn will resend
    // instead of the app-wide default it was last seeded with.

    @Test
    fun `lastContextProject reads project off a context event, which decodes as Unknown`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"client":"aura-android","project":"Assistant"}}"""),
        )
        assertEquals("Assistant", SessionEvent.lastContextProject(events))
    }

    @Test
    fun `lastContextProject round-trips a managed-project context key verbatim`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"project":"managed:9f3a"}}"""),
        )
        assertEquals("managed:9f3a", SessionEvent.lastContextProject(events))
    }

    @Test
    fun `lastContextProject picks the MOST RECENT context event, never merges across several`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"project":"OldProject"}}"""),
            SessionEvent.decode(json, """{"type":"user","ts":"t2","payload":{"text":"hi"}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t3","payload":{"project":"Assistant"}}"""),
        )
        assertEquals("Assistant", SessionEvent.lastContextProject(events))
    }

    @Test
    fun `lastContextProject is null when the session has no context event at all`() {
        val events = listOf(SessionEvent.decode(json, """{"type":"user","ts":"t1","payload":{"text":"hi"}}"""))
        assertEquals(null, SessionEvent.lastContextProject(events))
    }

    @Test
    fun `lastContextProject is null when the most recent context event has no project field`() {
        // A Temporary session (no project) must round-trip back to null (=> Temporary), NEVER leak a
        // project set two turns ago forward - mirrors backend _load_last_context winning verbatim.
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"project":"Assistant"}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t2","payload":{"model":"claude-sonnet-5"}}"""),
        )
        assertEquals(null, SessionEvent.lastContextProject(events))
    }

    @Test
    fun `lastContextProject treats a blank project field the same as absent`() {
        val events = listOf(SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"project":""}}"""))
        assertEquals(null, SessionEvent.lastContextProject(events))
    }

    @Test
    fun `lastContextProject follows a mid-transcript switch_project, not the project the turn opened in`() {
        // The auto-select flow: the app sends the "auto" sentinel with the query, the model calls
        // switch_project, and the switch persists its own context event naming where it moved to.
        // The newest context event wins, so the session's project is the SWITCHED one - the value
        // the next /query must resend, or the turn after this migrates back to a scratch cwd.
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"client":"aura-android","project":"auto"}}"""),
            SessionEvent.decode(json, """{"type":"user","ts":"t2","payload":{"text":"fix the login bug"}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t3","payload":{"project":"acme/beacon","cwd":"/srv/acme/beacon"}}"""),
        )
        assertEquals("acme/beacon", SessionEvent.lastContextProject(events))
    }

    @Test
    fun `a session that switches twice reports the LAST project, so one task can span several`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"project":"auto"}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t2","payload":{"project":"acme/beacon","cwd":"/srv/acme/beacon"}}"""),
            SessionEvent.decode(json, """{"type":"tool_result","ts":"t3","payload":{"tool_id":"shell","operation":"run","success":true}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t4","payload":{"project":"managed:9f3a","cwd":"/srv/wt/9f3a"}}"""),
        )
        assertEquals("managed:9f3a", SessionEvent.lastContextProject(events))
    }

    // --- the per-event readers: what keeps the project LIVE during a running turn ---
    //
    // ChatViewModel folds one event at a time as it streams in, so it cannot re-scan a settled
    // transcript. adoptContextProject is the seam it uses; the two facts it has to keep apart are
    // "a context event naming no project" (the session is on a temp-dir cwd) and "not a context
    // event at all" (nothing to adopt) - a bare null from contextProject collapses them, and
    // collapsing them would blank the project on the first agent_message_delta of every turn.

    @Test
    fun `adoptContextProject takes the project a context event names`() {
        val event = SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"project":"acme/beacon","cwd":"/srv/acme/beacon"}}""")
        assertEquals("acme/beacon", SessionEvent.adoptContextProject(event, current = "auto"))
    }

    @Test
    fun `adoptContextProject leaves the project alone for every non-context event`() {
        val streamed = listOf(
            """{"type":"agent_message_delta","ts":"t1","payload":{"text":"working","agent_id":"a1","depth":0}}""",
            """{"type":"tool_result","ts":"t2","payload":{"tool_id":"shell","operation":"run","success":true}}""",
            """{"type":"assistant","ts":"t3","payload":{"text":"done"}}""",
            """{"type":"completion","ts":"t4","payload":{"done":true}}""",
        ).map { SessionEvent.decode(json, it) }

        streamed.forEach { event ->
            assertEquals(
                "an ordinary streamed event must never blank the session's project",
                "acme/beacon",
                SessionEvent.adoptContextProject(event, current = "acme/beacon"),
            )
        }
    }

    @Test
    fun `adoptContextProject clears the project when a context event names none - that IS Temporary`() {
        val event = SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"client":"aura-android"}}""")
        assertEquals(null, SessionEvent.adoptContextProject(event, current = "acme/beacon"))
    }

    @Test
    fun `adoptContextProject with no event at all is a pass-through`() {
        // ChatViewModel's error path publishes without an originating event.
        assertEquals("acme/beacon", SessionEvent.adoptContextProject(null, current = "acme/beacon"))
    }

    @Test
    fun `folding events one at a time lands exactly where lastContextProject does`() {
        // The live path and the session-load path must agree by construction, not by two readers
        // happening to be written the same way.
        val events = listOf(
            """{"type":"context","ts":"t1","payload":{"project":"auto"}}""",
            """{"type":"agent_message_delta","ts":"t2","payload":{"text":"looking","agent_id":"a1","depth":0}}""",
            """{"type":"context","ts":"t3","payload":{"project":"acme/beacon","cwd":"/srv/acme/beacon"}}""",
            """{"type":"assistant","ts":"t4","payload":{"text":"done"}}""",
        ).map { SessionEvent.decode(json, it) }

        val folded = events.fold<SessionEvent, String?>(null) { current, event ->
            SessionEvent.adoptContextProject(event, current)
        }
        assertEquals(SessionEvent.lastContextProject(events), folded)
        assertEquals("acme/beacon", folded)
    }

    @Test
    fun `isContextEvent is true only for a persisted context frame`() {
        assertTrue(SessionEvent.isContextEvent(SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{}}""")))
        assertFalse(SessionEvent.isContextEvent(SessionEvent.decode(json, """{"type":"user","ts":"t1","payload":{"text":"hi"}}""")))
        // An unrecognized future type also decodes to Unknown - the type string is what decides.
        assertFalse(SessionEvent.isContextEvent(SessionEvent.decode(json, """{"type":"brand_new","ts":"t1","payload":{}}""")))
    }

    @Test
    fun `a malformed project field degrades to null instead of throwing on the live path`() {
        // The `jsonPrimitive` accessor THROWS on a nested object/array. On the history path that
        // landed in a try/catch; on the per-event live path it would take the run's collector down,
        // so these readers use `as? JsonPrimitive` throughout.
        val nested = SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"project":{"key":"acme"}}}""")
        val array = SessionEvent.decode(json, """{"type":"context","ts":"t2","payload":{"project":["acme"]}}""")

        assertEquals(null, SessionEvent.contextProject(nested))
        assertEquals(null, SessionEvent.contextProject(array))
        assertEquals(null, SessionEvent.lastContextProject(listOf(nested, array)))
        assertEquals(null, SessionEvent.adoptContextProject(nested, current = "acme/beacon"))
    }

    @Test
    fun `a malformed model field degrades the same way`() {
        val nested = SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"model":{"id":"x"}}}""")
        assertEquals(null, SessionEvent.lastContextModel(listOf(nested)))
    }

    // --- lastContextMcpTools (ChatViewModel.bind's session tool-narrowing hydration) ---
    // THREE-STATE, matching what ComposerScope.mcpToolsForContext writes: an ABSENT field means "all
    // tools bound" (untouched) and round-trips to null so the next /query omits it again; an EMPTY
    // array is an explicit ceiling of zero and round-trips to the empty set so the next /query
    // re-declares it. Reading empty as absent re-widens an all-tools-off session one turn later.

    @Test
    fun `lastContextMcpTools reads the allowlist off a context event as a set`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"project":"Assistant","mcp_tools":["wiki_ask","scg_search"]}}"""),
        )
        assertEquals(setOf("wiki_ask", "scg_search"), SessionEvent.lastContextMcpTools(events))
    }

    @Test
    fun `lastContextMcpTools picks the MOST RECENT context event, never merges across several`() {
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"mcp_tools":["old_tool"]}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t2","payload":{"mcp_tools":["wiki_ask"]}}"""),
        )
        assertEquals(setOf("wiki_ask"), SessionEvent.lastContextMcpTools(events))
    }

    @Test
    fun `lastContextMcpTools is null when the most recent context event omitted the field`() {
        // Omitted => backend binds every tool => untouched => null (not an empty allowlist).
        val events = listOf(
            SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"mcp_tools":["wiki_ask"]}}"""),
            SessionEvent.decode(json, """{"type":"context","ts":"t2","payload":{"project":"Assistant"}}"""),
        )
        assertEquals(null, SessionEvent.lastContextMcpTools(events))
    }

    @Test
    fun `lastContextMcpTools reads an EMPTY allowlist array as an explicit ceiling of zero`() {
        // NOT null. A session whose newest context event declares `mcp_tools: []` asked for no MCP
        // tools; hydrating that as "untouched" would make the very next send omit the field and
        // re-bind the whole registry, with nothing on screen to show the ceiling was dropped.
        val events = listOf(SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"mcp_tools":[]}}"""))
        assertEquals(emptySet<String>(), SessionEvent.lastContextMcpTools(events))
    }

    @Test
    fun `an all-tools-off session round-trips its own empty ceiling back onto the wire`() {
        // The contract joined end to end: what lastContextMcpTools READS off the persisted event
        // feeds ComposerScope.activeToolIds, and what mcpToolsForContext WRITES has to be the same
        // declaration. Either half alone re-widens the session one turn later, and each half looks
        // correct in isolation, so the join is what has to be pinned.
        val events = listOf(SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"mcp_tools":[]}}"""))
        val hydrated = ComposerScope(activeToolIds = SessionEvent.lastContextMcpTools(events))

        assertEquals(emptyList<String>(), hydrated.mcpToolsForContext())
    }

    @Test
    fun `lastContextMcpTools is null when the session has no context event at all`() {
        val events = listOf(SessionEvent.decode(json, """{"type":"user","ts":"t1","payload":{"text":"hi"}}"""))
        assertEquals(null, SessionEvent.lastContextMcpTools(events))
    }

    // --- widget_ready ---

    @Test
    fun `widget_ready round-trips with both files, requirements and summary`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"widget_ready","ts":"2026-07-14T01:00:00.000000+00:00","payload":{"widget_id":"w1","session_id":"s1",""" +
                """"files":{"app.py":"import streamlit as st","data.json":"{\"n\":1}"},"requirements":["pandas"],"summary":"A chart"}}""",
        )
        val widget = event as SessionEvent.WidgetReady
        assertEquals("w1", widget.payload.widgetId)
        assertEquals("s1", widget.payload.sessionId)
        assertEquals("import streamlit as st", widget.payload.files.appPy)
        assertEquals("""{"n":1}""", widget.payload.files.dataJson)
        assertEquals(listOf("pandas"), widget.payload.requirements)
        assertEquals("A chart", widget.payload.summary)
    }

    @Test
    fun `widget_ready with no requirements or summary decodes with defaults`() {
        val event = SessionEvent.decode(
            json,
            """{"type":"widget_ready","ts":"t1","payload":{"widget_id":"w1","session_id":"s1","files":{"app.py":"x","data.json":"{}"}}}""",
        )
        val widget = event as SessionEvent.WidgetReady
        assertTrue(widget.payload.requirements.isEmpty())
        assertEquals(null, widget.payload.summary)
    }

    @Test
    fun `a widget_ready missing a required file degrades to Unknown rather than a half-formed widget`() {
        // files.data.json absent → WidgetFiles can't decode → falls through the resilient net to
        // Unknown (dropped by the reducer), never a widget rendered with an empty script/data.
        val event = SessionEvent.decode(
            json,
            """{"type":"widget_ready","ts":"t1","payload":{"widget_id":"w1","session_id":"s1","files":{"app.py":"x"}}}""",
        )
        val unknown = event as SessionEvent.Unknown
        assertEquals("widget_ready", unknown.type)
    }
}
