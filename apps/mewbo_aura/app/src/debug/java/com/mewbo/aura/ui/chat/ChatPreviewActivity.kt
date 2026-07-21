package com.mewbo.aura.ui.chat

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.AttachmentSummary
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.ChatTodoItem
import com.mewbo.aura.data.model.QuestionResolution
import com.mewbo.aura.data.model.ToolCall
import com.mewbo.aura.data.model.UiAnswer
import com.mewbo.aura.data.model.UiQuestion
import com.mewbo.aura.data.model.UiQuestionOption
import com.mewbo.aura.ui.chat.toolcards.ToolCardRegistry
import com.mewbo.aura.ui.common.TypingIndicator
import com.mewbo.aura.ui.composer.AuraComposer
import com.mewbo.aura.ui.composer.ComposerState
import com.mewbo.aura.ui.composer.ComposerStyle
import com.mewbo.aura.ui.theme.AuraTheme
import kotlin.random.Random
import kotlinx.coroutines.delay
import kotlinx.serialization.json.Json

/**
 * Debug-only harness for [ChatSurface] - no Hilt, no network, a hand-built [ChatUiState] covering
 * the full chat item vocabulary (Task E verification; task-E-chat-brief.md). Not wired into any
 * nav graph or the release manifest.
 *
 * `runPhase = Streaming` with an already-open [ChatItem.AssistantMessage] is what a real run looks
 * like once the first delta lands - which is exactly why [TypingIndicator] (shown only *before*
 * that point, ui/CLAUDE.md) can't coexist with a live streaming row in one real state. This harness
 * renders [TypingIndicator] a second time, standalone, below the surface purely to prove the
 * component itself renders/animates - not to claim that combination happens in production.
 */
class ChatPreviewActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            AuraTheme {
                Scaffold { padding ->
                    Column(modifier = Modifier.fillMaxSize().padding(padding)) {
                        ChatSurface(
                            state = FakeChatState.value,
                            callbacks = ChatCallbacks(onSend = { _, _ -> }, onStop = {}, onRetry = {}, onMicTap = {}),
                            modifier = Modifier.weight(1f),
                        )
                        HorizontalDivider()
                        // Explicit weight (not just verticalScroll) - an unweighted scrollable
                        // Column measured against a weighted sibling can report its full natural
                        // content height back to the parent Column instead of the intended
                        // "whatever's left," starving the ChatSurface pane above to near-zero.
                        Column(modifier = Modifier.weight(1f).verticalScroll(rememberScrollState())) {
                            GreetingPreview()
                            HorizontalDivider()
                            ComponentGallery()
                            HorizontalDivider()
                            ComposerShowcase()
                        }
                    }
                }
            }
        }
    }
}

/**
 * Visual verification for [AuraComposer] (task W1-C, spec §6.2) - every state stacked so a single
 * screenshot proves pill geometry, morph endpoints, and bar layout at once, rather than needing to
 * script through transitions. Each row owns independent draft state so typing in one doesn't
 * affect the others.
 */
@Composable
private fun ComposerShowcase(modifier: Modifier = Modifier) {
    val fakeRms = rememberFakeDictationRms()
    var idleDraft by remember { mutableStateOf(TextFieldValue()) }
    var typingDraft by remember { mutableStateOf(TextFieldValue("Plan a weekend trip to Kyoto")) }
    var wrappedDraft by remember {
        mutableStateOf(
            TextFieldValue(
                "Plan a weekend trip to Kyoto, book a ryokan near Gion, find a highly rated kaiseki " +
                    "restaurant, and put together a two-day walking itinerary with at least three temples.",
            ),
        )
    }
    var streamingEmptyDraft by remember { mutableStateOf(TextFieldValue()) }
    var streamingDraft by remember { mutableStateOf(TextFieldValue("...and check flight prices too")) }

    Column(modifier = modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(text = "Composer showcase (debug only) - spec §6.2 C1-C4, Rev E §E-3 anatomy", style = MaterialTheme.typography.labelSmall)

        ComposerShowcaseRow("C1 Idle") {
            AuraComposer(
                state = ComposerState.Idle,
                draft = idleDraft,
                onDraftChange = { idleDraft = it },
                onSend = {}, onStop = {}, onMicTap = {}, onDictationStop = {}, onVoiceModeTap = {},
            )
        }
        ComposerShowcaseRow("C2 Typing") {
            AuraComposer(
                state = ComposerState.Typing,
                draft = typingDraft,
                onDraftChange = { typingDraft = it },
                onSend = {}, onStop = {}, onMicTap = {}, onDictationStop = {}, onVoiceModeTap = {},
            )
        }
        ComposerShowcaseRow("C2 Typing - wrapped multi-line draft") {
            AuraComposer(
                state = ComposerState.Typing,
                draft = wrappedDraft,
                onDraftChange = { wrappedDraft = it },
                onSend = {}, onStop = {}, onMicTap = {}, onDictationStop = {}, onVoiceModeTap = {},
            )
        }
        ComposerShowcaseRow("C3 Dictation - live bars (fake RMS)") {
            AuraComposer(
                state = ComposerState.Dictation(rmsDb = fakeRms, partialText = null),
                draft = TextFieldValue(),
                onDraftChange = {},
                onSend = {}, onStop = {}, onMicTap = {}, onDictationStop = {}, onVoiceModeTap = {},
            )
        }
        ComposerShowcaseRow("C3 Dictation - partial transcript") {
            AuraComposer(
                state = ComposerState.Dictation(rmsDb = fakeRms, partialText = "flights to Kyoto next week"),
                draft = TextFieldValue(),
                onDraftChange = {},
                onSend = {}, onStop = {}, onMicTap = {}, onDictationStop = {}, onVoiceModeTap = {},
            )
        }
        ComposerShowcaseRow("C4 Streaming - empty draft (stop)") {
            AuraComposer(
                state = ComposerState.Streaming(hasDraft = false),
                draft = streamingEmptyDraft,
                onDraftChange = { streamingEmptyDraft = it },
                onSend = {}, onStop = {}, onMicTap = {}, onDictationStop = {}, onVoiceModeTap = {},
            )
        }
        ComposerShowcaseRow("C4 Streaming - queued send") {
            AuraComposer(
                state = ComposerState.Streaming(hasDraft = true),
                draft = streamingDraft,
                onDraftChange = { streamingDraft = it },
                onSend = {}, onStop = {}, onMicTap = {}, onDictationStop = {}, onVoiceModeTap = {},
            )
        }
        ComposerShowcaseRow("C5 FloatingOverlay style (Docked composer, W3 supplies its own trailingAccessory)") {
            AuraComposer(
                state = ComposerState.Idle,
                draft = TextFieldValue(),
                onDraftChange = {},
                onSend = {}, onStop = {}, onMicTap = {}, onDictationStop = {}, onVoiceModeTap = {},
                style = ComposerStyle.FloatingOverlay,
            )
        }
    }
}

@Composable
private fun ComposerShowcaseRow(label: String, content: @Composable () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(text = label, style = MaterialTheme.typography.labelSmall)
        content()
    }
}

/** Fake RMS ticker (globals.md verify step: "fake RMS script") - redroid has no real mic input, so
 * this drives [AuraComposer]'s C3 bars with a plausible oscillating amplitude for screenshotting. */
@Composable
private fun rememberFakeDictationRms(): Float {
    var rms by remember { mutableFloatStateOf(0.3f) }
    LaunchedEffect(Unit) {
        while (true) {
            rms = 0.25f + 0.65f * Random.nextFloat()
            delay(220)
        }
    }
    return rms
}

/** Spec §6.6 empty-state (task W2-A verify step: "screenshot: greeting"). Fixed height since it
 * sits inside a scrolling column here rather than owning the full screen like it would in prod. */
@Composable
private fun GreetingPreview(modifier: Modifier = Modifier) {
    Column(modifier = modifier.padding(top = 12.dp)) {
        Text(text = "Greeting screen (debug only) - spec §6.6, empty transcript", style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(horizontal = 12.dp))
        ChatSurface(
            state = ChatUiState(displayName = "Krishna", items = emptyList(), runPhase = RunPhase.Idle),
            callbacks = ChatCallbacks(onSend = { _, _ -> }, onStop = {}, onRetry = {}),
            modifier = Modifier.height(GreetingPreviewHeight),
        )
    }
}

private val GreetingPreviewHeight = 600.dp

/**
 * Extra standalone component proof - see class doc for why this can't live inside the fake state.
 * [ToolCallGroupCard] is shown twice: one instance frozen in the [isRunActive]
 * "Using tools…" state, and one interactive instance the verify step taps through
 * collapsed -> expanded -> a row's own JSON detail open - the same [ChatUiState.items] fixture the
 * live [ChatSurface] uses would need real chat-transcript scroll to reach, which this scrollable
 * gallery column sidesteps entirely.
 */
@Composable
private fun ComponentGallery(modifier: Modifier = Modifier) {
    Column(modifier = modifier.padding(12.dp)) {
        Text(text = "Component gallery (debug only)", style = MaterialTheme.typography.labelSmall)
        TypingIndicator(modifier = Modifier.padding(top = 8.dp), label = "Mewbo is responding")

        Text(text = "Tool call group - running", style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 16.dp))
        ToolCallGroupCard(item = ToolCallGroupPreview.value, isRunActive = true, modifier = Modifier.padding(top = 4.dp))

        Text(text = "Tool call group - tap to expand, tap a row for its JSON", style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 16.dp))
        ToolCallGroupCard(item = ToolCallGroupPreview.value, isRunActive = false, modifier = Modifier.padding(top = 4.dp))

        // All three ToolCardRegistry paths in one screenshot - the registered renderer, and the two
        // routes into the generic fallback (an id with no renderer; a registered id whose args don't
        // parse). The fallback is the reason PromotedTools can grow without ui/ work, so it needs to
        // be as visible here as the happy path.
        Text(text = "Action card - device_set_alarm (registered renderer)", style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 16.dp))
        ToolCardRegistry.Render(call = ToolCardPreview.alarm, modifier = Modifier.padding(top = 4.dp))

        Text(text = "Action card - alarm with no message arg", style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 16.dp))
        ToolCardRegistry.Render(call = ToolCardPreview.alarmNoMessage, modifier = Modifier.padding(top = 4.dp))

        Text(text = "Action card - allowlisted id with NO renderer yet (generic fallback)", style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 16.dp))
        ToolCardRegistry.Render(call = ToolCardPreview.unregistered, modifier = Modifier.padding(top = 4.dp))

        Text(text = "Action card - alarm with unparseable args (degrades to generic)", style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 16.dp))
        ToolCardRegistry.Render(call = ToolCardPreview.alarmBadArgs, modifier = Modifier.padding(top = 4.dp))
    }
}

/** The four [ToolCardRegistry] outcomes, shared by the gallery above and [FakeChatState] below. */
private object ToolCardPreview {
    val alarm = alarmCall(
        ts = "2026-07-01T10:00:11Z",
        inputJson = """{"hour":8,"minute":0,"message":"Hit the gym"}""",
        summary = "Handed off an 8:00 AM alarm to the clock app.",
    )

    val alarmNoMessage = alarmCall(
        ts = "2026-07-01T10:00:12Z",
        inputJson = """{"hour":18,"minute":45}""",
        summary = "Handed off a 6:45 PM alarm to the clock app.",
    )

    /** A registered id whose args are the wrong SHAPE - model output is a black box, so this path
     * is real, not theoretical. Must render the generic card, never a half-empty alarm. */
    val alarmBadArgs = alarmCall(
        ts = "2026-07-01T10:00:13Z",
        inputJson = """{"hour":"tomorrow morning"}""",
        summary = "Set an alarm.",
    )

    /**
     * What a tool added to `PromotedTools` with no ui/ renderer YET would look like - the id is
     * humanized into the header and the backend's summary carries the payload. This is the fixture
     * that keeps the "add an id, get a working card for free" promise honest, so it deliberately
     * uses a REAL catalog tool (`device_set_timer`, `DeviceToolCatalog`) that is simply not
     * allowlisted today - an invented id would prove nothing about the next card anyone actually adds.
     */
    val unregistered = ToolCall(
        toolId = "device_set_timer",
        operation = "write",
        inputJson = Json.parseToJsonElement("""{"seconds":600,"label":"Pasta"}"""),
        summary = "Started a 10-minute timer.",
        success = true,
        detail = "ok",
        error = null,
        model = null,
        ts = "2026-07-01T10:00:14Z",
        key = "tool_result:2026-07-01T10:00:14Z:device_set_timer",
    )

    private fun alarmCall(ts: String, inputJson: String, summary: String) = ToolCall(
        toolId = "device_set_alarm",
        operation = "write",
        inputJson = Json.parseToJsonElement(inputJson),
        summary = summary,
        success = true, // the reducer only ever promotes a SUCCESSFUL call
        detail = "ok",
        error = null,
        model = null,
        ts = ts,
        // Derived, never hand-passed alongside a separate ts: the real key IS a function of the ts
        // (TranscriptReducer builds "tool_result:{ts}:{toolId}"), and letting the two be supplied
        // independently is how a fixture ends up with three distinct keys all stamped one timestamp.
        key = "tool_result:$ts:device_set_alarm",
    )
}

/** Same 3-call shape as [FakeChatState] (success/rich-input, failure/error, success/null-input) -
 * shared here so the two showcase instances above and the scripted transcript stay in sync. */
private object ToolCallGroupPreview {
    val value = ChatItem.ToolCallGroup(
        key = "tool_result:2026-07-01T10:00:02Z:web_search",
        calls = listOf(
            ToolCall(
                toolId = "web_search",
                operation = "search",
                inputJson = Json.parseToJsonElement(
                    """{"query":"Tokyo weather tomorrow","max_results":3,"sources":["jma.go.jp","accuweather.com"]}""",
                ),
                summary = "Found 3 sources for Tokyo's forecast tomorrow.",
                success = true,
                detail = "{\"query\": \"Tokyo weather tomorrow\", \"results\": 3, \"top_result\": \"jma.go.jp\"}",
                error = null,
                model = "gpt-5",
                ts = "2026-07-01T10:00:02Z",
                key = "tool_result:2026-07-01T10:00:02Z:web_search",
            ),
            ToolCall(
                toolId = "fetch_news",
                operation = "read",
                inputJson = Json.parseToJsonElement("""{"source":"news-aggregator.internal"}"""),
                summary = "Upstream news aggregator failed after 3 retries.",
                success = false,
                detail = null,
                error = "HTTP 503 from news-aggregator.internal after 3 retries (backoff 0.5s -> 4s).",
                model = null,
                ts = "2026-07-01T10:00:03Z",
                key = "tool_result:2026-07-01T10:00:03Z:fetch_news",
            ),
            ToolCall(
                toolId = "update_todos",
                operation = "write",
                inputJson = null,
                summary = "Replaced the checklist with 3 items.",
                success = true,
                detail = "ok",
                error = null,
                model = null,
                ts = "2026-07-01T10:00:03Z",
                key = "tool_result:2026-07-01T10:00:03Z:update_todos",
            ),
        ),
    )
}

/**
 * Covers the full [ChatItem] vocabulary (task W2-A verify step) in one scripted transcript:
 * - turn 1 (completed, first) -> shows the disclaimer, NOT the action row (not the last completed turn)
 * - one [ChatItem.ToolCallGroup] covering all three [ToolCall] shapes: success
 *   with rich JSON input, failure with an error, and a null-input call
 * - both [ChatItem.AgentChip] states (terminal-success + still in-progress)
 * - [ChatItem.TodoList] (Plan card)
 * - a [ChatItem.ToolCard] (promoted `device_set_alarm` action card) as a turn's WHOLE response -
 *   directly under its user bubble, with no assistant prose after it
 * - a `pending` [ChatItem.UserBubble] (queued-send 70% opacity, spec §6.12)
 * - turn 2 (completed, LAST completed) -> shows the action row, with [ChatUiState.speakingKey] set
 *   to it so the read-aloud active state (accentPrimary tint + surfaceIconScrim circle) is visible
 * - [ChatItem.ErrorCard] with Retry
 * - turn 3, currently streaming -> [com.mewbo.aura.ui.common.WordFadeText]'s comet tail, no action row yet
 */
private object FakeChatState {
    val value = ChatUiState(
        title = "Tokyo trip planning",
        runPhase = RunPhase.Streaming,
        speakingKey = "assistant:2",
        items = listOf(
            ChatItem.UserBubble(
                text = "What's the weather like in Tokyo tomorrow?",
                ts = "2026-07-01T10:00:00Z",
                // Attachment tile row fixture (metadata-only, no real image) - exercises both the
                // image-glyph and short-extension-label branches of AttachmentTile/AttachmentGlyphs.
                attachments = listOf(
                    AttachmentSummary(filename = "tokyo-skyline.png", mimeType = "image/png", sizeBytes = 812_000L),
                    AttachmentSummary(filename = "itinerary-draft.pdf", mimeType = "application/pdf", sizeBytes = 154_000L),
                ),
                key = "user:1",
            ),
            ChatItem.AssistantMessage(
                text = "Tokyo looks partly cloudy tomorrow with a high near 22°C and a gentle breeze.",
                isStreaming = false,
                ts = "2026-07-01T10:00:01Z",
                key = "assistant:1",
            ),
            // Same 3-call fixture ComponentGallery's standalone showcase uses (success/rich-input,
            // failure/error, success/null-input) - defined once as ToolCallGroupPreview.value.
            ToolCallGroupPreview.value,
            ChatItem.AgentChip(
                agentId = "a1",
                agentType = "researcher",
                status = "completed",
                stepsCompleted = 5,
                terminal = true,
                success = true,
                ts = "2026-07-01T10:00:04Z",
                key = "agent:a1",
            ),
            ChatItem.AgentChip(
                agentId = "a2",
                agentType = "fact-checker",
                status = "running",
                stepsCompleted = 2,
                terminal = false,
                success = null,
                ts = "2026-07-01T10:00:05Z",
                key = "agent:a2",
            ),
            ChatItem.TodoList(
                items = listOf(
                    ChatTodoItem(label = "Check Tokyo forecast", status = "completed"),
                    ChatTodoItem(label = "Pull top headlines", status = "in_progress"),
                    ChatTodoItem(label = "Summarize both for the user", status = "pending"),
                ),
                ts = "2026-07-01T10:00:06Z",
            ),
            // A promoted tool's turn, stacked the way the reducer REALLY emits it: bubble -> card ->
            // prose. The card does NOT replace the narration (the wire sends exactly one `assistant`
            // per turn and the reducer never drops it, DESIGN.md §6) - this fixture must keep showing
            // the prose, or the gallery teaches the next card's author a turn shape that does not
            // exist. Sitting directly under a user bubble it also proves ChatTranscript's
            // isChipFamily gap covers ChatItem.ToolCard - miss that and it renders flush at 0dp.
            ChatItem.UserBubble(
                text = "Set an alarm for 8am to hit the gym",
                ts = "2026-07-01T10:00:11Z",
                key = "user:alarm",
            ),
            ChatItem.ToolCard(call = ToolCardPreview.alarm, key = "toolcard:${ToolCardPreview.alarm.key}"),
            ChatItem.AssistantMessage(
                text = "Alarm set for 8:00 AM — the request was handed off to your clock app.",
                isStreaming = false,
                ts = "2026-07-01T10:00:12Z",
                key = "assistant:alarm",
            ),
            // A PENDING ask-user question: a single-select with options + a free-text
            // question, its own Submit. The interactive card the agent blocks on.
            ChatItem.UserBubble(
                text = "Help me wire up authentication",
                ts = "2026-07-01T10:00:13Z",
                key = "user:q1",
            ),
            ChatItem.Question(
                callId = "q-preview-1",
                callToken = "tok-1",
                questions = listOf(
                    UiQuestion(
                        header = "Auth method",
                        question = "Which authentication method should I wire up?",
                        options = listOf(
                            UiQuestionOption(label = "OAuth 2.0 (Recommended)", description = "Delegated login via an identity provider"),
                            UiQuestionOption(label = "API keys", description = "Static per-client tokens"),
                            UiQuestionOption(label = "Session cookies", description = null),
                        ),
                        multiSelect = false,
                    ),
                    UiQuestion(header = "Notes", question = "Anything else I should know?", options = emptyList(), multiSelect = false),
                ),
                resolution = null,
                ts = "2026-07-01T10:00:14Z",
                key = "question:q-preview-1",
            ),
            // A SETTLED ask-user question answered on ANOTHER surface — read-only, no error residue.
            ChatItem.UserBubble(text = "Which database?", ts = "2026-07-01T10:00:15Z", key = "user:q2"),
            ChatItem.Question(
                callId = "q-preview-2",
                callToken = "tok-2",
                questions = listOf(
                    UiQuestion(
                        header = "Database",
                        question = "Which database should I target?",
                        options = listOf(
                            UiQuestionOption(label = "PostgreSQL", description = null),
                            UiQuestionOption(label = "SQLite", description = null),
                        ),
                        multiSelect = false,
                    ),
                ),
                resolution = QuestionResolution.Answered(
                    answers = listOf(UiAnswer(selectedIndexes = listOf(0), text = null)),
                    answeredVia = "console",
                ),
                ts = "2026-07-01T10:00:16Z",
                key = "question:q-preview-2",
            ),
            ChatItem.UserBubble(
                text = "Can you also check flight prices?",
                ts = "2026-07-01T10:00:07Z",
                pending = true,
                key = "user:2",
            ),
            ChatItem.AssistantMessage(
                text = "Sure! Let me check flight prices for you as well.",
                isStreaming = false,
                ts = "2026-07-01T10:00:08Z",
                key = "assistant:2",
            ),
            ChatItem.ErrorCard(
                message = "Lost connection while checking flight prices",
                ts = "2026-07-01T10:00:09Z",
                key = "error:1",
            ),
            ChatItem.AssistantMessage(
                text = "I wasn't able to pull today's headlines just now - the news source timed out, so I'll",
                isStreaming = true,
                ts = "2026-07-01T10:00:10Z",
                key = "assistant:3",
            ),
        ),
    )
}
