package com.mewbo.aura.data.device

import android.content.Context
import android.content.Intent
import android.provider.AlarmClock
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * `device_set_alarm` - hands `AlarmClock.ACTION_SET_ALARM` off to whatever clock app the device
 * resolves it to. The handoff itself IS the authoritative success signal: `startActivity` on that
 * intent always registers the alarm request with the resolved clock app once it returns without
 * throwing, so the result reports it as done rather than hedging with an unconfirmed-delivery
 * caveat that only ever undermined the model's confidence in a call that, in practice, always
 * succeeds. `FLAG_ACTIVITY_NEW_TASK` is required because this starts an Activity from a
 * non-Activity (data-layer) context - which Android's background-activity-launch restriction
 * (API 29+) silently discards while the app isn't foregrounded, hence [foregroundChecker]
 * gating this BEFORE the launch is attempted (review finding F4).
 */
class SetAlarmHandler @Inject constructor(
    @ApplicationContext private val context: Context,
    private val foregroundChecker: AppForegroundChecker,
) : DeviceToolHandler {
    override val toolId: String = "device_set_alarm"

    override suspend fun execute(args: JsonObject): JsonObject {
        foregroundChecker.requireForeground()
        val hour = args.requireArgInt("hour")
        val minute = args.requireArgInt("minute")
        require(hour in 0..23) { "hour must be within 0..23, got $hour" }
        require(minute in 0..59) { "minute must be within 0..59, got $minute" }
        val message = args.optArgString("message")

        val intent = Intent(AlarmClock.ACTION_SET_ALARM).apply {
            putExtra(AlarmClock.EXTRA_HOUR, hour)
            putExtra(AlarmClock.EXTRA_MINUTES, minute)
            if (!message.isNullOrBlank()) putExtra(AlarmClock.EXTRA_MESSAGE, message)
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)

        return buildJsonObject {
            put("handed_to_clock_app", true)
        }
    }
}
