package com.mewbo.aura.data.device

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * `device_wake` - schedules [AlarmManager.setAlarmClock] a few hundred ms out so the OS treats it
 * as a user-facing alarm clock (Doze-exempt, fires promptly even with the screen off) rather than
 * a deferrable/batchable alarm; [WakeAlarmReceiver] does the actual ringing/vibrating once it
 * fires. `showIntent` is deliberately `null` (the SDK documents it nullable) rather than pointing
 * at `MainActivity` - reaching into `ui/` from `data/` would violate the strict data-never-imports-
 * ui layering (apps/mewbo_aura/CLAUDE.md).
 */
class WakeDeviceHandler @Inject constructor(
    @ApplicationContext private val context: Context,
) : DeviceToolHandler {
    override val toolId: String = "device_wake"

    override suspend fun execute(args: JsonObject): JsonObject {
        val reason = args.optArgString("reason")
        val alarmManager = context.getSystemService(AlarmManager::class.java)
            ?: throw IllegalStateException("AlarmManager unavailable")

        val receiverIntent = Intent(context, WakeAlarmReceiver::class.java).apply {
            if (!reason.isNullOrBlank()) putExtra(WakeAlarmReceiver.EXTRA_REASON, reason)
        }
        val operation = PendingIntent.getBroadcast(
            context,
            WAKE_REQUEST_CODE,
            receiverIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val triggerAtMillis = System.currentTimeMillis() + TRIGGER_LEAD_MS
        try {
            alarmManager.setAlarmClock(AlarmManager.AlarmClockInfo(triggerAtMillis, null), operation)
        } catch (e: SecurityException) {
            // setAlarmClock's user-facing-alarm semantics exempt it from the SCHEDULE_EXACT_ALARM
            // "Alarms & reminders" special-access gate other exact-alarm APIs need (lint's
            // ScheduleExactAlarm check still wants this handled explicitly regardless), but an OEM
            // could still refuse it - surface that as a normal handler_error rather than crashing.
            throw IllegalStateException("Device refused to schedule the wake alarm: ${e.message}", e)
        }

        return buildJsonObject {
            put("scheduled", true)
            put("note", "A wake alarm was scheduled to ring and vibrate the device shortly.")
        }
    }

    private companion object {
        const val TRIGGER_LEAD_MS = 500L
        const val WAKE_REQUEST_CODE = 4271
    }
}
