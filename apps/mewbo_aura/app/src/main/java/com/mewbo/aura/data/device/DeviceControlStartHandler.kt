package com.mewbo.aura.data.device

import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * `device_control_start` — ask for control of the screen.
 *
 * **This tool is never gated on the grant, because it is how you get one.** It
 * is also not gated on Shizuku being live: a refusal that names the cause is
 * the entire value here, and a tool withheld because the thing it explains is
 * missing explains nothing.
 *
 * A refusal reports `status: "ok"` with an outcome, not a tool error. The call
 * did what it was asked — it asked, and the answer was no — and an error
 * envelope would put the reason in the one field a model is most likely to
 * treat as transport noise and retry.
 */
class DeviceControlStartHandler @Inject constructor(
    private val session: DeviceControlSession,
) : DeviceToolHandler {
    override val toolId: String = "device_control_start"

    override suspend fun execute(args: JsonObject): JsonObject = session.start().let { grant ->
        buildJsonObject {
            put("outcome", grant.code)
            put("active", grant.active)
            put("message", grant.message)
        }
    }
}
