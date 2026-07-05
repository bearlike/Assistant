package com.mewbo.aura.data.device

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** `device_get_battery` - no args, no permission. Reads the sticky `ACTION_BATTERY_CHANGED`
 * broadcast via the well-known `registerReceiver(null, filter)` one-shot idiom rather than a
 * registered receiver instance - there's nothing to unregister. */
class BatteryStatusHandler @Inject constructor(
    @ApplicationContext private val context: Context,
) : DeviceToolHandler {
    override val toolId: String = "device_get_battery"

    override suspend fun execute(args: JsonObject): JsonObject {
        val status = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = status?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = status?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val percent = if (level >= 0 && scale > 0) (level * 100) / scale else -1
        val chargeStatus = status?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val charging = chargeStatus == BatteryManager.BATTERY_STATUS_CHARGING ||
            chargeStatus == BatteryManager.BATTERY_STATUS_FULL
        return buildJsonObject {
            put("level_percent", percent)
            put("charging", charging)
        }
    }
}
