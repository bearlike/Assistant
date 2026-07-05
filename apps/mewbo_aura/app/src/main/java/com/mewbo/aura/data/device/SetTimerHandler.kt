package com.mewbo.aura.data.device

import android.content.Context
import android.content.Intent
import android.provider.AlarmClock
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** `device_set_timer` - same handoff-not-confirmation honesty as [SetAlarmHandler] (task brief):
 * `AlarmClock.ACTION_SET_TIMER` reaches whatever clock app the device resolves it to, and
 * `EXTRA_SKIP_UI` is honored inconsistently across OEMs, so there is no reliable "the timer now
 * exists" signal to report. Same [foregroundChecker] gate as [SetAlarmHandler] (review finding F4). */
class SetTimerHandler @Inject constructor(
    @ApplicationContext private val context: Context,
    private val foregroundChecker: AppForegroundChecker,
) : DeviceToolHandler {
    override val toolId: String = "device_set_timer"

    override suspend fun execute(args: JsonObject): JsonObject {
        foregroundChecker.requireForeground()
        val seconds = args.requireArgInt("seconds")
        require(seconds >= 1) { "seconds must be >= 1, got $seconds" }
        val label = args.optArgString("label")

        val intent = Intent(AlarmClock.ACTION_SET_TIMER).apply {
            putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            if (!label.isNullOrBlank()) putExtra(AlarmClock.EXTRA_MESSAGE, label)
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)

        return buildJsonObject {
            put("handed_to_clock_app", true)
            put("note", "The timer request was handed to the device's clock app; whether it was actually started was not confirmed.")
        }
    }
}
