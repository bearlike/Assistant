package com.mewbo.aura.data.device

import java.time.Instant
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [SmsInboxReader] is the injectable seam over `ContentResolver`/`Cursor` (apps/mewbo_aura/CLAUDE.md
 * - neither constructible in a plain-JVM test), so the decisions that are actually this handler's -
 * page-size clamping, the look-ahead that produces `has_more`, offset pass-through, direction
 * mapping - are pinned here against an in-memory pool.
 *
 * The fixture is the shape of the real failure this tool was rewritten for: a long conversation
 * with one person, buried under a handful of NEWER unrelated messages. A global page cannot show
 * both, so what matters is that the result SAYS it did not.
 */
class ReadLatestSmsHandlerTest {

    /** Newest-first, matching [SmsInboxReader.queryMessages]' contract. Index 0..4 are the newest
     * and belong to five different senders; 5..16 are one twelve-message conversation with
     * `+15557778888`, three of them the user's own replies. */
    private val mailbox: List<SmsMessageRow> = buildList {
        val base = Instant.parse("2026-07-01T00:00:00Z").toEpochMilli()
        listOf("Receipt for your order", "Payment overdue", "URGENT account locked", "Package shipped", "50% off today")
            .forEachIndexed { index, body ->
                add(SmsMessageRow("+1555000000$index", body, base + (100 - index) * 3_600_000L, outbound = false))
            }
        (0 until 12).forEach { index ->
            add(
                SmsMessageRow(
                    address = "+15557778888",
                    body = "thread ${11 - index}",
                    timestampEpochMillis = base + (11 - index) * 3_600_000L,
                    outbound = index % 4 == 0,
                ),
            )
        }
    }

    /** Faithful in-memory model of the seam contract: NARROW first, then skip, then take - the
     * order the production reader gets from a single SQL selection. Filtering after a fixed pool
     * is precisely the bug, so a fake that did it in the other order would test nothing. */
    private class FakeReader(private val pool: List<SmsMessageRow>) : SmsInboxReader {
        var lastFilter: String? = null
        var lastOffset: Int = -1
        var lastLimit: Int = -1

        override fun queryMessages(senderFilter: String?, offset: Int, limit: Int): List<SmsMessageRow> {
            lastFilter = senderFilter
            lastOffset = offset
            lastLimit = limit
            val matching = if (senderFilter.isNullOrEmpty()) pool else pool.filter { it.address.contains(senderFilter, ignoreCase = true) }
            return matching.drop(offset).take(limit)
        }
    }

    private fun handlerOver(pool: List<SmsMessageRow>) = FakeReader(pool).let { it to ReadLatestSmsHandler(it) }

    private fun JsonObject.messages() = getValue("messages").jsonArray
    private fun JsonObject.hasMore() = getValue("has_more").jsonPrimitive.boolean
    private fun JsonObject.returned() = getValue("returned").jsonPrimitive.int
    private fun JsonObject.str(key: String) = get(key)?.jsonPrimitive?.content

    // --- the quiet failure: truncation must be visible -----------------------------------------

    @Test
    fun `a truncated page reports has_more true`() = runTest {
        val (_, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 5) })

        assertEquals(5, result.messages().size)
        assertEquals(5, result.returned())
        assertTrue("a 5-of-17 page that claims completeness is the whole defect", result.hasMore())
    }

    @Test
    fun `a page covering everything reports has_more false`() = runTest {
        val (_, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 50) })

        assertEquals(17, result.messages().size)
        assertFalse(result.hasMore())
    }

    @Test
    fun `a page landing exactly on the last message reports has_more false, not true`() = runTest {
        val (_, handler) = handlerOver(mailbox.take(10))

        val result = handler.execute(buildJsonObject { put("count", 10) })

        assertEquals(10, result.messages().size)
        assertFalse("an exact-fit page has nothing beyond it - the look-ahead row must be absent", result.hasMore())
    }

    @Test
    fun `an empty mailbox is an empty page, not an error, and claims no more`() = runTest {
        val (_, handler) = handlerOver(emptyList())

        val result = handler.execute(buildJsonObject {})

        assertTrue(result.messages().isEmpty())
        assertEquals(0, result.returned())
        assertFalse(result.hasMore())
    }

    @Test
    fun `the look-ahead row is never leaked into messages`() = runTest {
        val (reader, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 3) })

        assertEquals("the reader is asked for one MORE than the page size", 4, reader.lastLimit)
        assertEquals(3, result.messages().size)
    }

    // --- paging --------------------------------------------------------------------------------

    @Test
    fun `offset pages to older messages and is echoed back`() = runTest {
        val (reader, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 5); put("offset", 5) })

        assertEquals(5, reader.lastOffset)
        assertEquals(5, result.getValue("offset").jsonPrimitive.int)
        assertEquals("thread 11", result.messages()[0].jsonObject.str("body"))
    }

    @Test
    fun `paging the whole mailbox reaches every message exactly once`() = runTest {
        val (_, handler) = handlerOver(mailbox)

        val seen = mutableListOf<String>()
        var offset = 0
        do {
            val page = handler.execute(buildJsonObject { put("count", 4); put("offset", offset) })
            page.messages().forEach { seen += it.jsonObject.str("body")!! }
            offset += 4
        } while (page.hasMore())

        assertEquals(mailbox.map { it.body }, seen)
    }

    @Test
    fun `a negative offset is clamped to the newest page rather than failing`() = runTest {
        val (reader, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 2); put("offset", -7) })

        assertEquals(0, reader.lastOffset)
        assertEquals(0, result.getValue("offset").jsonPrimitive.int)
    }

    @Test
    fun `an offset past the end is an empty page, not an error`() = runTest {
        val (_, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 5); put("offset", 900) })

        assertTrue(result.messages().isEmpty())
        assertFalse(result.hasMore())
    }

    // --- page size -----------------------------------------------------------------------------

    @Test
    fun `default count is 10, not 1`() = runTest {
        val (_, handler) = handlerOver(mailbox)

        assertEquals(10, handler.execute(buildJsonObject {}).messages().size)
    }

    @Test
    fun `count clamps into the shared window's bounds`() = runTest {
        val big = (1..80).map { SmsMessageRow("+1555", "m$it", it.toLong(), outbound = false) }
        val (_, handler) = handlerOver(big)

        assertEquals(DeviceReadWindow.SMS.maxCount, handler.execute(buildJsonObject { put("count", 9_999) }).messages().size)
        assertEquals(1, handler.execute(buildJsonObject { put("count", 0) }).messages().size)
        assertEquals(1, handler.execute(buildJsonObject { put("count", -4) }).messages().size)
        assertEquals(7, handler.execute(buildJsonObject { put("count", 7) }).messages().size)
    }

    // --- narrowing to one conversation ---------------------------------------------------------

    @Test
    fun `sender_filter reaches the reader so narrowing happens BEFORE paging`() = runTest {
        val (reader, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 50); put("sender_filter", "7778888") })

        assertEquals("7778888", reader.lastFilter)
        assertEquals("the whole conversation, none of the newer noise", 12, result.messages().size)
        assertFalse(result.hasMore())
        assertTrue(result.messages().all { it.jsonObject.str("address") == "+15557778888" })
    }

    @Test
    fun `a blank sender_filter is treated as absent, never as a filter matching nothing`() = runTest {
        val (reader, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 3); put("sender_filter", "   ") })

        assertNull(reader.lastFilter)
        assertEquals(3, result.messages().size)
    }

    @Test
    fun `sender_filter is trimmed and matches case-insensitively as a substring`() = runTest {
        val named = listOf(SmsMessageRow("Mom", "hi", 2L, outbound = false), SmsMessageRow("+15551112222", "ad", 1L, outbound = false))
        val (_, handler) = handlerOver(named)

        val result = handler.execute(buildJsonObject { put("count", 10); put("sender_filter", " mom ") })

        assertEquals(1, result.messages().size)
        assertEquals("Mom", result.messages()[0].jsonObject.str("address"))
    }

    @Test
    fun `sender_filter matching nothing is an empty page, not an error`() = runTest {
        val (_, handler) = handlerOver(mailbox)

        val result = handler.execute(buildJsonObject { put("count", 5); put("sender_filter", "nobody-matches-this") })

        assertTrue(result.messages().isEmpty())
        assertFalse(result.hasMore())
    }

    // --- direction + row shape -----------------------------------------------------------------

    @Test
    fun `direction distinguishes the user's own replies from the other party's messages`() = runTest {
        val (_, handler) = handlerOver(mailbox)

        val thread = handler.execute(buildJsonObject { put("count", 50); put("sender_filter", "7778888") }).messages()

        val directions = thread.map { it.jsonObject.str("direction") }
        assertEquals(3, directions.count { it == "outbound" })
        assertEquals(9, directions.count { it == "inbound" })
        // The FIRST thread row is the user's own reply - reported as `from: +15557778888` by the
        // old shape, which reads as the other party having said it.
        assertEquals("outbound", directions[0])
    }

    @Test
    fun `each message maps address-direction-body-timestamp with an ISO-8601 instant`() = runTest {
        val row = SmsMessageRow("+15559876543", "the newest", Instant.parse("2026-07-03T00:00:00Z").toEpochMilli(), outbound = false)
        val (_, handler) = handlerOver(listOf(row))

        val message = handler.execute(buildJsonObject {}).messages()[0].jsonObject

        assertEquals("+15559876543", message.str("address"))
        assertEquals("inbound", message.str("direction"))
        assertEquals("the newest", message.str("body"))
        assertEquals(Instant.parse("2026-07-03T00:00:00Z"), Instant.parse(message.str("timestamp")!!))
        assertNull("`from` lies on an outbound row and must not come back", message["from"])
    }
}
