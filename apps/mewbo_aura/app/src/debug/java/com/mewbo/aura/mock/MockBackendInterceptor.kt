package com.mewbo.aura.mock

import java.io.IOException
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.asResponseBody
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import okio.Pipe
import okio.buffer

/**
 * Debug-only, OkHttp-layer scripted backend (Gitea #181 follow-up: on-device overlay/gate testing
 * was firing a real LLM query on every assist-gesture invocation - real cost, multiplying with
 * auto-listen). Contributed to the app-wide `OkHttpClient` via Hilt's `Set<Interceptor>`
 * multibinding ([com.mewbo.aura.di.DataModule.provideOkHttpClient]) rather than a `chain.proceed()`
 * wrapper, so it fully short-circuits every intercepted call - zero real network traffic while
 * [flags] is on, and since it's added as an APPLICATION interceptor (not a network one), it can
 * synthesize a [Response] without OkHttp ever opening a socket.
 *
 * Applies transparently to BOTH Retrofit's [com.mewbo.aura.data.api.AuraApi] calls AND
 * [com.mewbo.aura.data.sse.SessionStreamClient]'s SSE connection - the latter's `EventSource`
 * client is built via `okHttpClient.newBuilder()` in `DataModule.provideEventSourceFactory`, which
 * COPIES the base client's interceptor chain, so this needs no separate wiring for streaming.
 *
 * Only installed on the classpath at all in the debug variant (`di/MockBackendModule.kt`); release
 * never sees this file. [flags] is checked at the START of every intercepted call (not baked into
 * whether this interceptor is present at all) - mirrors [com.mewbo.aura.voice.VoiceBackends]'
 * runtime-checked-not-DI-graph-checked toggle shape, so flipping the Settings row takes effect on
 * the very next request with no process restart.
 */
@Singleton
class MockBackendInterceptor @Inject constructor(
    private val flags: MockBackendFlags,
    private val mockSessionStore: MockSessionStore,
) : Interceptor {

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        if (!flags.isEnabledBlocking()) return chain.proceed(request)

        val segments = request.url.pathSegments
        return when {
            request.method == "POST" && segments == SESSIONS_PATH -> handleCreate(request)
            request.method == "GET" && segments == SESSIONS_PATH -> handleList(request)
            request.method == "POST" && isSessionSubPath(segments, "query") -> handleQuery(request, segments[2])
            request.method == "POST" && isSessionSubPath(segments, "message") -> handleSteer(request, segments[2])
            request.method == "PATCH" && isSessionSubPath(segments, "title") -> handleRename(request, segments[2])
            request.method == "POST" && isSessionSubPath(segments, "archive") -> handleArchive(request, segments[2])
            request.method == "GET" && isSessionSubPath(segments, "stream") -> handleStream(request, segments[2])
            request.method == "GET" && isSessionSubPath(segments, "events") -> handleEvents(request, segments[2])
            request.method == "GET" && segments == MODELS_PATH -> handleModels(request)
            request.method == "GET" && segments == PROJECTS_PATH -> handleProjects(request)
            request.method == "GET" && segments == TOOLS_PATH -> handleTools(request)
            else -> unhandled(request)
        }
    }

    private fun isSessionSubPath(segments: List<String>, leaf: String): Boolean =
        segments.size == 4 && segments[0] == "api" && segments[1] == "sessions" && segments[3] == leaf

    // ---- POST api/sessions ----

    private fun handleCreate(request: Request): Response {
        val record = mockSessionStore.create()
        return jsonResponse(request, 200, buildJsonObject { put("session_id", record.sessionId) })
    }

    // ---- GET api/sessions ----

    private fun handleList(request: Request): Response {
        mockSessionStore.seedDemoIfEmpty()
        val includeArchived = request.url.queryParameter("include_archived")?.toBoolean() ?: false
        val sessions = mockSessionStore.list(includeArchived).map { record ->
            buildJsonObject {
                put("session_id", record.sessionId)
                put("title", record.title)
                // "running" (never the status string) is the real liveness signal (data/CLAUDE.md) -
                // status only ever takes {idle, completed, failed, awaiting_approval, canceled} on
                // the real backend; mirror that vocabulary here rather than inventing "running".
                put("status", if (record.scenario == null) "idle" else "completed")
                put("running", record.running)
                put("recoverable", record.scenario != null && !record.running)
                // Provenance the rail's scope filter keys on (mirrors the real SessionOrigin value).
                put("origin", record.origin)
                put("created_at", record.createdAt)
                put("updated_at", record.createdAt)
            }
        }
        return jsonResponse(request, 200, buildJsonObject { put("sessions", JsonArray(sessions)) })
    }

    // ---- POST api/sessions/{id}/query ----

    private fun handleQuery(request: Request, sessionId: String): Response {
        val queryText = extractField(request, "query")
        mockSessionStore.startTurn(sessionId, queryText)
        return jsonResponse(
            request,
            202,
            buildJsonObject {
                put("session_id", sessionId)
                put("accepted", true)
            },
        )
    }

    // ---- POST api/sessions/{id}/message (steer) ----

    private fun handleSteer(request: Request, sessionId: String): Response {
        val text = extractField(request, "text")
        mockSessionStore.startTurn(sessionId, text)
        return jsonResponse(
            request,
            202,
            buildJsonObject {
                put("session_id", sessionId)
                put("enqueued", true)
            },
        )
    }

    // ---- PATCH api/sessions/{id}/title ----

    /** Mirrors the real backend's trim + 120-char cap + blank-rejection (task brief), so the
     * rename sheet's error path is exercisable against the mock backend too. */
    private fun handleRename(request: Request, sessionId: String): Response {
        val title = extractField(request, "title").trim()
        if (title.isEmpty()) {
            return jsonResponse(request, 400, buildJsonObject { put("error", "title must not be blank") })
        }
        if (mockSessionStore.get(sessionId) == null) {
            return jsonResponse(request, 404, buildJsonObject { put("error", "unknown session") })
        }
        val capped = title.take(TITLE_MAX_CHARS)
        mockSessionStore.rename(sessionId, capped)
        return jsonResponse(
            request,
            200,
            buildJsonObject {
                put("session_id", sessionId)
                put("title", capped)
            },
        )
    }

    // ---- POST api/sessions/{id}/archive ----

    private fun handleArchive(request: Request, sessionId: String): Response {
        if (mockSessionStore.get(sessionId) == null) {
            return jsonResponse(request, 404, buildJsonObject { put("error", "unknown session") })
        }
        mockSessionStore.archive(sessionId)
        return jsonResponse(
            request,
            200,
            buildJsonObject {
                put("session_id", sessionId)
                put("archived", true)
            },
        )
    }

    // ---- GET api/sessions/{id}/stream (SSE) ----

    /** Streams the session's scripted [MockScenarios.Scenario] frames through a piped
     * [okio.BufferedSource] with REAL pacing (a background [Thread] sleeping [Pair.first]ms
     * before writing each frame) - a fixed all-at-once body would defeat the point of testing the
     * real streaming/word-fade UI. Terminal `stream_end` (no `ts`/`payload` on the wire,
     * `data/CLAUDE.md`) is appended here rather than scripted in [MockScenarios], since it's a
     * transport control frame, not a genuine [com.mewbo.aura.data.model.SessionEvent]. */
    private fun handleStream(request: Request, sessionId: String): Response {
        val scenarioFrames = mockSessionStore.get(sessionId)?.scenario?.frames.orEmpty()
        val pipe = Pipe(PIPE_BUFFER_BYTES)
        val sink = pipe.sink.buffer()

        Thread({
            try {
                for ((delayMs, frame) in scenarioFrames) {
                    Thread.sleep(delayMs)
                    sink.writeUtf8("data: ${frame}\n\n")
                    sink.flush()
                }
                sink.writeUtf8("data: {\"type\":\"stream_end\"}\n\n")
                sink.flush()
            } catch (e: IOException) {
                // The reader closed early (dismiss()/stopStreaming() cancelled the OkHttp call) -
                // nothing further to write; the finally block below still cleans up.
            } catch (e: InterruptedException) {
                // ditto, via Thread.interrupt() on a cancelled call.
            } finally {
                runCatching { sink.close() }
                mockSessionStore.finishTurn(sessionId)
            }
        }, "MockBackendInterceptor-sse-$sessionId").start()

        val body = pipe.source.buffer().asResponseBody("text/event-stream".toMediaType())
        return Response.Builder()
            .request(request)
            .protocol(Protocol.HTTP_1_1)
            .code(200)
            .message("OK")
            .header("Content-Type", "text/event-stream")
            .body(body)
            .build()
    }

    // ---- GET api/sessions/{id}/events ----

    private fun handleEvents(request: Request, sessionId: String): Response {
        val record = mockSessionStore.get(sessionId)
        val events = mockSessionStore.eventsFor(sessionId)
        return jsonResponse(
            request,
            200,
            buildJsonObject {
                put("session_id", sessionId)
                put("events", JsonArray(events))
                put("running", record?.running ?: false)
                put("status", if (record?.scenario == null) "idle" else "completed")
                put("title", record?.title)
                put("recoverable", record?.scenario != null && record.running == false)
            },
        )
    }

    // ---- GET api/models / api/projects / api/tools: small fixed catalogs ----

    private fun handleModels(request: Request): Response = jsonResponse(
        request,
        200,
        buildJsonObject {
            putJsonArray("models") {
                add("anthropic/claude-sonnet-5")
                add("anthropic/claude-haiku-4-5")
            }
            put("default", "anthropic/claude-sonnet-5")
            putJsonObject("capabilities") {
                putJsonObject("anthropic/claude-sonnet-5") { put("supports_vision", true) }
                putJsonObject("anthropic/claude-haiku-4-5") { put("supports_vision", false) }
            }
        },
    )

    private fun handleProjects(request: Request): Response = jsonResponse(
        request,
        200,
        buildJsonObject {
            putJsonArray("projects") {
                add(
                    buildJsonObject {
                        put("name", "mock-project")
                        put("available", true)
                        put("source", "config")
                        put("is_worktree", false)
                    },
                )
            }
        },
    )

    private fun handleTools(request: Request): Response = jsonResponse(
        request,
        200,
        buildJsonObject {
            putJsonArray("tools") {
                add(
                    buildJsonObject {
                        put("tool_id", "mock_tool")
                        put("name", "Mock Tool")
                        put("kind", "builtin")
                        put("enabled", true)
                    },
                )
            }
        },
    )

    /** Anything not explicitly routed above (attachments upload, device-tool-result posting) - a
     * clear, honest signal rather than a fake 200 in a shape callers don't actually expect
     * (`ignoreUnknownKeys` masks a WRONG-but-decodable shape just as easily as a right one). */
    private fun unhandled(request: Request): Response = jsonResponse(
        request,
        501,
        buildJsonObject { put("error", "mock_backend: unhandled path ${request.method} ${request.url.encodedPath}") },
    )

    private fun jsonResponse(request: Request, code: Int, json: JsonObject): Response = Response.Builder()
        .request(request)
        .protocol(Protocol.HTTP_1_1)
        .code(code)
        .message(if (code in 200..299) "OK" else "Mock Backend")
        .body(json.toString().toResponseBody("application/json".toMediaType()))
        .build()

    /** `RequestBody` is write-only by design (OkHttp) - buffer it once to read a single top-level
     * string field back out. Never throws: a malformed/absent body degrades to `""`, matching how
     * every mock handler above tolerates a blank query/steer text (still starts a turn, just with
     * the default [MockScenarios.happyPath]). */
    private fun extractField(request: Request, field: String): String {
        val buffer = Buffer()
        request.body?.writeTo(buffer)
        return runCatching {
            Json.parseToJsonElement(buffer.readUtf8()).jsonObject[field]?.jsonPrimitive?.content
        }.getOrNull().orEmpty()
    }

    private companion object {
        val SESSIONS_PATH = listOf("api", "sessions")
        val MODELS_PATH = listOf("api", "models")
        val PROJECTS_PATH = listOf("api", "projects")
        val TOOLS_PATH = listOf("api", "tools")

        /** Small - these are short scripted turns, not real long-running streams; a generous but
         * bounded pipe buffer avoids the writer thread ever blocking mid-scenario. */
        const val PIPE_BUFFER_BYTES = 64L * 1024L

        /** Mirrors the real backend's rename cap (task brief: "server trims + caps at 120 chars"). */
        const val TITLE_MAX_CHARS = 120
    }
}
