package com.mewbo.aura.data.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive

/**
 * One [DeviceToolHandler] per `device_*` tool id ([DeviceToolCatalog.ALL]); [DeviceToolExecutor]
 * maps a call's `tool_id` to the matching instance. An [execute] that throws a [DeviceToolError]
 * surfaces its own [DeviceToolError.code]; any OTHER thrown exception (including the
 * [requireArgInt]/[optArgString] helpers below, which throw plain [IllegalArgumentException]) is
 * caught by the executor and reported as the generic `handler_error`.
 */
interface DeviceToolHandler {
    val toolId: String
    suspend fun execute(args: JsonObject): JsonObject
}

/** Thrown by a handler to report a SPECIFIC error [code] instead of the executor's catch-all
 * `handler_error` - e.g. `invalid_args` ([DeviceToolArgsException]) or `app_not_foreground`
 * ([requireForeground], review finding F4). One common base so [DeviceToolExecutor] only needs a
 * single extra `catch` clause ahead of its generic one, not one per code. */
sealed class DeviceToolError(val code: String, message: String) : Exception(message)

/** Thrown by a handler to signal malformed/insufficient args specifically - distinct from a
 * generic failure so [DeviceToolExecutor] reports `code:"invalid_args"` instead of the catch-all
 * `handler_error` (Gitea #179 Phase 4: `device_dismiss_alarm`'s per-search-mode required args). */
class DeviceToolArgsException(message: String) : DeviceToolError("invalid_args", message)

/** Single-method seam over whether the app process is currently foregrounded - checked (via
 * [requireForeground]) before any handler calls `context.startActivity` from a non-Activity
 * context, since Android's background-activity-launch restriction (API 29+) silently discards
 * such a launch while backgrounded. Without this, a handler would report `handed_to_clock_app:
 * true` for a launch that never actually reached the screen (review finding F4). */
fun interface AppForegroundChecker {
    fun isForeground(): Boolean
}

/** Thrown by [requireForeground] - a launch attempted while the app isn't foregrounded (review
 * finding F4). */
class AppNotForegroundException :
    DeviceToolError("app_not_foreground", "Aura must be open on screen to hand this to the clock app")

/** Shared guard for every handler that hands off to the clock app via an activity `Intent`
 * ([SetAlarmHandler], [SetTimerHandler], [DismissAlarmHandler]) - call as the FIRST step of
 * [DeviceToolHandler.execute], before touching `Intent`/`Context.startActivity` at all. */
internal fun AppForegroundChecker.requireForeground() {
    if (!isForeground()) throw AppNotForegroundException()
}

/** Reads a required integer arg out of a [DeviceToolCall][com.mewbo.aura.data.model.DeviceToolCallPayload]'s
 * `args` object; throws [IllegalArgumentException] (surfaces as `handler_error`) when missing or
 * not an integer. */
internal fun JsonObject.requireArgInt(key: String): Int {
    val element = this[key] ?: throw IllegalArgumentException("Missing required arg '$key'")
    return element.jsonPrimitive.intOrNull ?: throw IllegalArgumentException("Arg '$key' must be an integer")
}

/** Same contract as [requireArgInt] but for a required, non-blank string arg. */
internal fun JsonObject.requireArgString(key: String): String {
    val value = optArgString(key)
    if (value.isNullOrBlank()) throw IllegalArgumentException("Missing required arg '$key'")
    return value
}

internal fun JsonObject.optArgString(key: String): String? = this[key]?.jsonPrimitive?.contentOrNull

internal fun JsonObject.optArgInt(key: String): Int? = this[key]?.jsonPrimitive?.intOrNull
