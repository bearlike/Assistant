package com.mewbo.aura.data.device

import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * `device_control_stop` — release control of the screen.
 *
 * Idempotent by contract, and reported as such: `released` distinguishes "this
 * call ended a grant" from "there was nothing to end", but BOTH are `ok`. The
 * automatic release paths (a run reaching its terminal event, the notification's
 * Stop action) race an explicit call by design, so a stop that finds nothing
 * held is the ordinary case rather than a mistake worth reporting as one.
 *
 * Ungated for the same reason [DeviceControlStartHandler] is: a session must
 * always be able to end control, including one that has just lost the
 * conditions that let it start.
 */
class DeviceControlStopHandler @Inject constructor(
    private val session: DeviceControlSession,
) : DeviceToolHandler {
    override val toolId: String = "device_control_stop"

    override suspend fun execute(args: JsonObject): JsonObject {
        val released = session.stop()
        return buildJsonObject {
            put("released", released)
            put("active", false)
            put(
                "message",
                if (released) {
                    "Device control released. The screen tools will refuse until you start again."
                } else {
                    "Device control was not active. Nothing to release."
                },
            )
        }
    }
}
