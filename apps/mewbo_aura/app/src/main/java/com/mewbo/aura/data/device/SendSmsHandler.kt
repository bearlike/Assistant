package com.mewbo.aura.data.device

import android.content.Context
import android.telephony.SmsManager
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** `device_send_sms` - `SmsManager.divideMessage`/`sendMultipartTextMessage` handle long bodies
 * transparently (a short body is just a one-part multipart send). Result is honest that this is a
 * hand-off to the radio, not a delivery confirmation (task brief) - `SmsManager` offers delivery
 * intents but wiring them into this request/response-shaped tool call would need a second,
 * out-of-band result POST this wire contract doesn't support. */
class SendSmsHandler @Inject constructor(
    @ApplicationContext private val context: Context,
) : DeviceToolHandler {
    override val toolId: String = "device_send_sms"

    override suspend fun execute(args: JsonObject): JsonObject {
        val to = args.requireArgString("to")
        val body = args.requireArgString("body")

        val smsManager = context.getSystemService(SmsManager::class.java)
            ?: throw IllegalStateException("SmsManager unavailable")
        val parts = smsManager.divideMessage(body)
        smsManager.sendMultipartTextMessage(to, null, parts, null, null)

        return buildJsonObject {
            put("status", "sent")
            put("note", "The message was handed to the radio for sending; delivery was not confirmed.")
        }
    }
}
