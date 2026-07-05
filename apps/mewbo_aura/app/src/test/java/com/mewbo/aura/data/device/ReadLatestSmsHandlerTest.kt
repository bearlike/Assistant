package com.mewbo.aura.data.device

import java.time.Instant
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** [SmsInboxReader] is the injectable seam over `ContentResolver`/`Cursor`
 * (apps/mewbo_aura/CLAUDE.md - neither constructible in a plain-JVM test), so
 * [ReadLatestSmsHandler]'s count-clamping/sender-filter decision logic is tested here against a
 * plain in-memory fake pool, newest-first (matching what the real reader guarantees). */
class ReadLatestSmsHandlerTest {

    // Newest-first, matching the [SmsInboxReader.queryInbox] contract the real
    // ContentResolverSmsInboxReader guarantees (`DATE DESC`) - the handler trusts that ordering
    // and just takes off the front, so a correctly-ordered fixture is load-bearing here.
    private val pool = listOf(
        SmsMessageRow(from = "+15559876543", body = "first (newest)", receivedAtEpochMillis = Instant.parse("2026-07-03T00:00:00Z").toEpochMilli()),
        SmsMessageRow(from = "Mom", body = "second", receivedAtEpochMillis = Instant.parse("2026-07-02T00:00:00Z").toEpochMilli()),
        SmsMessageRow(from = "+15551234567", body = "third", receivedAtEpochMillis = Instant.parse("2026-07-01T00:00:00Z").toEpochMilli()),
    )

    @Test
    fun `default count is 1 and returns only the newest message`() = runTest {
        val handler = ReadLatestSmsHandler(SmsInboxReader { pool })

        val result = handler.execute(buildJsonObject {})

        val messages = result["messages"]!!.jsonArray
        assertEquals(1, messages.size)
        assertEquals("first (newest)", messages[0].jsonObject["body"]?.jsonPrimitive?.content)
    }

    @Test
    fun `count above 5 clamps down to 5`() = runTest {
        val bigPool = (1..10).map { SmsMessageRow(from = "x", body = "msg$it", receivedAtEpochMillis = it.toLong()) }
        val handler = ReadLatestSmsHandler(SmsInboxReader { bigPool })

        val result = handler.execute(buildJsonObject { put("count", 99) })

        assertEquals(5, result["messages"]!!.jsonArray.size)
    }

    @Test
    fun `count below 1 clamps up to 1`() = runTest {
        val handler = ReadLatestSmsHandler(SmsInboxReader { pool })

        val result = handler.execute(buildJsonObject { put("count", 0) })

        assertEquals(1, result["messages"]!!.jsonArray.size)
    }

    @Test
    fun `count within range is honored exactly`() = runTest {
        val handler = ReadLatestSmsHandler(SmsInboxReader { pool })

        val result = handler.execute(buildJsonObject { put("count", 2) })

        val messages = result["messages"]!!.jsonArray
        assertEquals(2, messages.size)
        assertEquals("first (newest)", messages[0].jsonObject["body"]?.jsonPrimitive?.content)
        assertEquals("second", messages[1].jsonObject["body"]?.jsonPrimitive?.content)
    }

    @Test
    fun `sender_filter matches case-insensitively as a substring`() = runTest {
        val handler = ReadLatestSmsHandler(SmsInboxReader { pool })

        val result = handler.execute(buildJsonObject { put("count", 5); put("sender_filter", "mom") })

        val messages = result["messages"]!!.jsonArray
        assertEquals(1, messages.size)
        assertEquals("Mom", messages[0].jsonObject["from"]?.jsonPrimitive?.content)
    }

    @Test
    fun `sender_filter matching nothing returns an empty messages array, not an error`() = runTest {
        val handler = ReadLatestSmsHandler(SmsInboxReader { pool })

        val result = handler.execute(buildJsonObject { put("count", 5); put("sender_filter", "nobody-matches-this") })

        assertTrue(result["messages"]!!.jsonArray.isEmpty())
    }

    @Test
    fun `each message maps from-body-received_at with an ISO-8601 timestamp`() = runTest {
        val handler = ReadLatestSmsHandler(SmsInboxReader { pool })

        val result = handler.execute(buildJsonObject {})

        val message = result["messages"]!!.jsonArray[0].jsonObject
        assertEquals("+15559876543", message["from"]?.jsonPrimitive?.content)
        assertEquals("first (newest)", message["body"]?.jsonPrimitive?.content)
        assertEquals(Instant.parse("2026-07-03T00:00:00Z"), Instant.parse(message["received_at"]!!.jsonPrimitive.content))
    }

    @Test
    fun `queries the pool bounded, not the caller's requested count`() = runTest {
        var requestedMax = -1
        val handler = ReadLatestSmsHandler(SmsInboxReader { maxRows -> requestedMax = maxRows; pool })

        handler.execute(buildJsonObject { put("count", 1) })

        // The pool bound is an internal implementation detail (enough headroom for a sender
        // filter to still have candidates) - just confirm it's NOT literally clamped to 1, i.e.
        // the filter/count split documented on the handler is real, not accidental.
        assertTrue(requestedMax > 1)
    }
}
