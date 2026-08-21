package com.mewbo.aura.data.device

import java.time.Instant
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * `device_read_latest_sms` - one page of the mailbox, newest-first, both directions.
 *
 * **The truncation signal is the point of this handler, not the messages.** A read that returns a
 * well-formed page with no way to tell it apart from the whole set is worse than one that fails:
 * measured, a global 5-message window answered a question about a 52-message conversation with
 * five unrelated marketing texts, and nothing in the response said so.
 *
 * That contract is deliberately NOT owned here - it lives in [DeviceReadPage] so the call-log and
 * contacts readers adopt it verbatim instead of each inventing a way to say "there is more". This
 * class owns only what is specific to SMS: reading the right table (both directions), narrowing to
 * one conversation, and rendering a row.
 *
 * Cost: `O(count)` rows in the response, hard-bounded by [DeviceReadWindow.SMS]; the read itself is
 * `O(collection)` in the provider (see [SmsInboxReader.queryMessages]) and paged by `offset`.
 *
 * Never logs a row's address/body - see [ContentResolverSmsInboxReader]'s doc.
 */
class ReadLatestSmsHandler @Inject constructor(
    private val inboxReader: SmsInboxReader,
) : DeviceToolHandler {
    override val toolId: String = "device_read_latest_sms"

    override suspend fun execute(args: JsonObject): JsonObject {
        val page = DeviceReadWindow.SMS.pageFrom(args)
        val senderFilter = args.optArgString("sender_filter")?.trim()?.takeIf { it.isNotEmpty() }

        val fetched = inboxReader.queryMessages(senderFilter, page.offset, page.fetchLimit)
        // No `total`: an exact one costs a second count over the whole mailbox on every call, and
        // `has_more` already answers the question that nearly produced a wrong answer.
        return page.envelope("messages", fetched) { row ->
            buildJsonObject {
                put("address", row.address)
                put("direction", if (row.outbound) "outbound" else "inbound")
                put("body", row.body)
                put("timestamp", Instant.ofEpochMilli(row.timestampEpochMillis).toString())
            }
        }
    }
}
