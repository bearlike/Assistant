package com.mewbo.aura.data.device

import android.content.Context
import android.content.Intent
import android.provider.AlarmClock
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** The extras [resolveDismissAlarmExtras] computes for `AlarmClock.ACTION_DISMISS_ALARM` -
 * [searchMode] is already the SDK `ALARM_SEARCH_MODE_*` constant value, not the schema's raw
 * `search_mode` string. Plain data, so the search-mode decision logic is unit-testable without a
 * real `Intent` (whose extras-bundle methods aren't safely callable in a plain-JVM test any more
 * than `android.net.Uri` is - apps/mewbo_aura/CLAUDE.md). */
internal data class DismissAlarmExtras(
    val searchMode: String,
    val hour: Int? = null,
    val minute: Int? = null,
    val label: String? = null,
)

/** `device_dismiss_alarm`'s validate-and-map step, split out of [DismissAlarmHandler.execute] so
 * it's testable without an `Intent`/`Context`. Every validation failure here - a missing/blank
 * `search_mode`, an unrecognized one, or a mode missing its required companion arg(s) - throws
 * [DeviceToolArgsException] (-> `code:"invalid_args"`), NOT the generic [requireArgString] (->
 * `handler_error`) other handlers use for their own required-arg checks (task brief's explicit
 * per-mode validation is scoped to this handler alone). */
internal fun resolveDismissAlarmExtras(args: JsonObject): DismissAlarmExtras {
    val searchMode = args.optArgString("search_mode")
    if (searchMode.isNullOrBlank()) throw DeviceToolArgsException("Missing required arg 'search_mode'")
    return when (searchMode) {
        "time" -> {
            val hour = args.optArgInt("hour") ?: throw DeviceToolArgsException("search_mode 'time' requires 'hour'")
            val minute = args.optArgInt("minute") ?: throw DeviceToolArgsException("search_mode 'time' requires 'minute'")
            if (hour !in 0..23) throw DeviceToolArgsException("hour must be within 0..23, got $hour")
            if (minute !in 0..59) throw DeviceToolArgsException("minute must be within 0..59, got $minute")
            DismissAlarmExtras(searchMode = AlarmClock.ALARM_SEARCH_MODE_TIME, hour = hour, minute = minute)
        }
        "label" -> {
            val label = args.optArgString("label")
            if (label.isNullOrBlank()) throw DeviceToolArgsException("search_mode 'label' requires 'label'")
            DismissAlarmExtras(searchMode = AlarmClock.ALARM_SEARCH_MODE_LABEL, label = label)
        }
        "next" -> DismissAlarmExtras(searchMode = AlarmClock.ALARM_SEARCH_MODE_NEXT)
        "all" -> DismissAlarmExtras(searchMode = AlarmClock.ALARM_SEARCH_MODE_ALL)
        else -> throw DeviceToolArgsException("search_mode must be one of time|label|next|all, got '$searchMode'")
    }
}

/** `device_dismiss_alarm` - hands `AlarmClock.ACTION_DISMISS_ALARM` off to whatever clock app the
 * device resolves it to; same handoff-not-confirmation honesty as [SetAlarmHandler]/[WakeDeviceHandler]
 * (task brief) - reliable on the stock clock app, varies by OEM elsewhere. Same [foregroundChecker] gate
 * as [SetAlarmHandler]/[SetTimerHandler] (review finding F4). */
class DismissAlarmHandler @Inject constructor(
    @ApplicationContext private val context: Context,
    private val foregroundChecker: AppForegroundChecker,
) : DeviceToolHandler {
    override val toolId: String = "device_dismiss_alarm"

    override suspend fun execute(args: JsonObject): JsonObject {
        foregroundChecker.requireForeground()
        val extras = resolveDismissAlarmExtras(args)
        val intent = Intent(AlarmClock.ACTION_DISMISS_ALARM).apply {
            putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, extras.searchMode)
            extras.hour?.let { putExtra(AlarmClock.EXTRA_HOUR, it) }
            extras.minute?.let { putExtra(AlarmClock.EXTRA_MINUTES, it) }
            extras.label?.let { putExtra(AlarmClock.EXTRA_MESSAGE, it) }
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)

        return buildJsonObject {
            put("handed_to_clock_app", true)
            put("note", "The dismiss request was handed to the device's clock app; behavior varies by OEM clock app.")
        }
    }
}
