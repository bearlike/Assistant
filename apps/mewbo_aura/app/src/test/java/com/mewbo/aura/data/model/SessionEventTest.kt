package com.mewbo.aura.data.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
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

    // --- lastContextMcpTools (ChatViewModel.bind's session tool-narrowing hydration) ---
    // `mcp_tools` is persisted ONLY when the user narrowed the tool set (non-empty allowlist); an
    // absent/empty field means "all tools bound" (untouched), which must round-trip back to null so
    // the next /query omits the field entirely (ComposerScope.mcpToolsForContext).

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
    fun `lastContextMcpTools treats an empty allowlist array the same as absent`() {
        val events = listOf(SessionEvent.decode(json, """{"type":"context","ts":"t1","payload":{"mcp_tools":[]}}"""))
        assertEquals(null, SessionEvent.lastContextMcpTools(events))
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
