package com.mewbo.aura.data.device

import java.time.Instant
import javax.inject.Inject
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** `device_read_latest_sms` - `count` clamped to 1..5 (schema default 1), `sender_filter` a
 * case-insensitive substring match against [SmsMessageRow.from]. Reads a bounded pool from
 * [inboxReader] (already newest-first) and applies both as pure Kotlin over that pool - the
 * decision logic under test in [ReadLatestSmsHandlerTest], independent of the real
 * `ContentResolver`. Never logs a row's `from`/`body` - see [ContentResolverSmsInboxReader]'s doc. */
class ReadLatestSmsHandler @Inject constructor(
    private val inboxReader: SmsInboxReader,
) : DeviceToolHandler {
    override val toolId: String = "device_read_latest_sms"

    override suspend fun execute(args: JsonObject): JsonObject {
        val count = (args.optArgInt("count") ?: DEFAULT_COUNT).coerceIn(MIN_COUNT, MAX_COUNT)
        val senderFilter = args.optArgString("sender_filter")?.trim()

        val pool = inboxReader.queryInbox(INBOX_POOL_SIZE)
        val filtered = if (senderFilter.isNullOrEmpty()) pool else pool.filter { it.from.contains(senderFilter, ignoreCase = true) }
        val messages = filtered.take(count).map { row ->
            buildJsonObject {
                put("from", row.from)
                put("body", row.body)
                put("received_at", Instant.ofEpochMilli(row.receivedAtEpochMillis).toString())
            }
        }
        return buildJsonObject { put("messages", JsonArray(messages)) }
    }

    private companion object {
        const val DEFAULT_COUNT = 1
        const val MIN_COUNT = 1
        const val MAX_COUNT = 5

        /** Pool size the [inboxReader] pulls before this handler's own filter/count logic runs -
         * generous enough that a sender filter still has plenty of newest-first candidates to
         * match against, without scanning the entire inbox on every call. */
        const val INBOX_POOL_SIZE = 200
    }
}
