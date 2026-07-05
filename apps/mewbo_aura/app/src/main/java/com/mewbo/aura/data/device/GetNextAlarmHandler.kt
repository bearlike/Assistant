package com.mewbo.aura.data.device

import java.time.Instant
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** `device_get_next_alarm` - no args, no permission. Android exposes no API to list every
 * scheduled alarm, only the single soonest one system-wide ([NextAlarmReader]); the catalog
 * description carries that limitation verbatim so the model doesn't over-claim (task brief). */
class GetNextAlarmHandler @Inject constructor(
    private val nextAlarmReader: NextAlarmReader,
) : DeviceToolHandler {
    override val toolId: String = "device_get_next_alarm"

    override suspend fun execute(args: JsonObject): JsonObject {
        val next = nextAlarmReader.nextAlarm()
        return buildJsonObject {
            if (next == null) {
                put("exists", false)
            } else {
                put("exists", true)
                put("trigger_at", Instant.ofEpochMilli(next.triggerAtEpochMillis).toString())
                if (next.ownerPackage != null) put("owner_package", next.ownerPackage)
            }
        }
    }
}
