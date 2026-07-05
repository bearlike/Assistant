package com.mewbo.aura.data.device

import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** `device_get_time` - no args, no permission. Returns the device's own local clock, not a
 * server-relative time - the whole point of a client-side tool. */
class DeviceTimeHandler @Inject constructor() : DeviceToolHandler {
    override val toolId: String = "device_get_time"

    override suspend fun execute(args: JsonObject): JsonObject {
        val zone = ZoneId.systemDefault()
        val now = ZonedDateTime.now(zone)
        return buildJsonObject {
            put("iso8601", now.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME))
            put("timezone", zone.id)
        }
    }
}
