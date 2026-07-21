package com.mewbo.aura.data.device

import android.app.ActivityManager
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
 * `handler_error` (`device_dismiss_alarm`'s per-search-mode required args). */
class DeviceToolArgsException(message: String) : DeviceToolError("invalid_args", message)

/**
 * Single-method seam over the ONE question every activity-launching handler must answer first:
 * **may this app start an activity for the user right now?** - checked via [requireForeground]
 * before any handler calls `context.startActivity` from a non-Activity context, since Android's
 * background-activity-launch restriction (API 29+) silently DISCARDS such a launch while the app
 * has no user-visible window. Without this, a handler would report `handed_to_clock_app: true` for
 * a launch that never actually reached the screen (review finding F4).
 *
 * The name is historical: it is NOT purely a process-importance test. See [canStartActivityNow] for
 * the actual composition and why the assist overlay counts.
 */
fun interface AppForegroundChecker {
    fun isForeground(): Boolean
}

/**
 * The activity-launch gate `di/DeviceModule` composes [AppForegroundChecker] out of. TRUE when
 * EITHER holds:
 *
 * - the app's own process importance is at/above `IMPORTANCE_FOREGROUND` (100) - a top Activity, the
 *   original and until now ONLY clause; or
 * - the assist overlay is currently on screen ([AssistOverlayPresence]).
 *
 * **The second clause is not a relaxation - the first clause is a BUG, confirmed against AOSP `main`
 * primary source (not inferred from docs).** Three facts, each independently load-bearing; do not
 * "simplify" this back to a plain importance check:
 *
 * 1. **The importance check provably fails while the session is showing.**
 *    `VoiceInteractionSessionConnection.showLocked()` rebinds the hosting service with
 *    `BIND_TREAT_LIKE_VISIBLE_FOREGROUND_SERVICE`, pushing the process to
 *    `PROCESS_STATE_FOREGROUND_SERVICE`. `ActivityManager.procStateToImportance()` maps that to
 *    `IMPORTANCE_FOREGROUND_SERVICE` (**125**) - only proc-states strictly better than
 *    FOREGROUND_SERVICE (TOP / BOUND_TOP / PERSISTENT) fall through to `IMPORTANCE_FOREGROUND`
 *    (100). Importance counts UP as it gets less important, so `125 <= 100` is false and the guard
 *    REFUSES a legitimately-showing session. That is exactly why `device_set_alarm`/`device_set_timer`/
 *    `device_dismiss_alarm` - the three activity-launching tools - returned `app_not_foreground` for
 *    every clock request made from the overlay.
 * 2. **The launch itself IS permitted there**, by TWO independent platform mechanisms, so allowing it
 *    is correct rather than a loophole: `WindowState` sets `mCallingUidHasNonAppVisibleWindow` for any
 *    window type `>= FIRST_SYSTEM_WINDOW` (excluding TOAST/PRIVATE_PRESENTATION), and
 *    `TYPE_VOICE_INTERACTION` is `FIRST_SYSTEM_WINDOW + 31`; `BackgroundActivityStartController`
 *    checks that flag unconditionally (no app-switch gating) and returns
 *    `BAL_ALLOW_NON_APP_VISIBLE_WINDOW`. Separately, the same `showLocked()` bind also carries
 *    `BIND_ALLOW_BACKGROUND_ACTIVITY_STARTS`. **Both exemptions live only while the session is SHOWN**
 *    - which is precisely the window [AssistOverlayPresence] tracks, and why that flag must never be
 *    stale-true (see its KDoc: a blocked launch is a SILENT no-op, so a stale flag makes a handler
 *    report `handed_to_clock_app: true` for an alarm that never reached the user).
 * 3. **Rejected alternative, do not reintroduce:** `VoiceInteractionSession.startAssistantActivity()`
 *    is the platform's first-class BAL-exempt API for this, but using it would mean plumbing the
 *    `VoiceInteractionSession` object down into these `data/device/` handlers - inverting the app's
 *    layering (`voice/` -> `data/`, never back up) for a case fact 2's visible-window exemption
 *    already covers - and it is API 34+. The presence flag is the smaller, correctly-layered fix.
 *
 * [processImportance] is nullable because `ActivityManager.runningAppProcesses` can legitimately
 * omit our own process; absent means "no evidence of foreground", not foreground.
 */
internal fun canStartActivityNow(processImportance: Int?, assistOverlayVisible: Boolean): Boolean =
    assistOverlayVisible ||
        (processImportance != null && processImportance <= ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND)

/** Thrown by [requireForeground] - a launch attempted while the app has no user-visible window
 * (review finding F4). */
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
