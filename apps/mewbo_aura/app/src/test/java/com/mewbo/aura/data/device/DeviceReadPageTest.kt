package com.mewbo.aura.data.device

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
 * The truncation contract is the deliverable, not an SMS implementation detail, so it is tested
 * WITHOUT any SMS type in sight. Every assertion here is one a call-log or contacts reader will
 * depend on verbatim; if this suite needed a `SmsMessageRow` to make its point, the contract would
 * have been SMS-shaped after all.
 */
class DeviceReadPageTest {

    private val window = DeviceReadWindow(defaultCount = 10, maxCount = 50)

    private fun JsonObject.hasMore() = getValue("has_more").jsonPrimitive.boolean
    private fun JsonObject.returned() = getValue("returned").jsonPrimitive.int
    private fun JsonObject.offset() = getValue("offset").jsonPrimitive.int

    /** Stands in for whatever row type a reader has - deliberately not an SMS row. */
    private fun rows(n: Int) = (1..n).map { "row$it" }
    private fun render(value: String) = buildJsonObject { put("value", value) }

    // --- the look-ahead, which is the whole mechanism ------------------------------------------

    @Test
    fun `fetchLimit is one more than the page so has_more costs no second query`() {
        assertEquals(11, DeviceReadPage(offset = 0, count = 10).fetchLimit)
        assertEquals(2, DeviceReadPage(offset = 99, count = 1).fetchLimit)
    }

    @Test
    fun `a full look-ahead means has_more and the extra row is trimmed`() {
        val page = DeviceReadPage(offset = 0, count = 3)

        val result = page.envelope("items", rows(page.fetchLimit), render = ::render)

        assertEquals(3, result.getValue("items").jsonArray.size)
        assertEquals(3, result.returned())
        assertTrue(result.hasMore())
    }

    @Test
    fun `an exactly-full page with no look-ahead row reports has_more false`() {
        val page = DeviceReadPage(offset = 0, count = 3)

        val result = page.envelope("items", rows(3), render = ::render)

        assertEquals(3, result.returned())
        assertFalse("an exact-fit page has nothing beyond it", result.hasMore())
    }

    @Test
    fun `a short page reports has_more false`() {
        val result = DeviceReadPage(offset = 0, count = 10).envelope("items", rows(2), render = ::render)

        assertEquals(2, result.returned())
        assertFalse(result.hasMore())
    }

    @Test
    fun `an empty page is well-formed, not an error`() {
        val result = DeviceReadPage(offset = 40, count = 10).envelope("items", emptyList<String>(), render = ::render)

        assertTrue(result.getValue("items").jsonArray.isEmpty())
        assertEquals(0, result.returned())
        assertEquals(40, result.offset())
        assertFalse(result.hasMore())
    }

    @Test
    fun `the look-ahead row is never rendered, so it cannot leak into the response`() {
        val rendered = mutableListOf<String>()
        val page = DeviceReadPage(offset = 0, count = 2)

        page.envelope("items", rows(3)) { value -> rendered += value; render(value) }

        assertEquals("the trim happens BEFORE render, structurally", listOf("row1", "row2"), rendered)
    }

    // --- the envelope every reader adopts verbatim ----------------------------------------------

    @Test
    fun `the three envelope fields are identical whatever the table is called`() {
        val messages = DeviceReadPage(offset = 0, count = 1).envelope("messages", rows(2), render = ::render)
        val calls = DeviceReadPage(offset = 0, count = 1).envelope("calls", rows(2), render = ::render)

        assertEquals(
            "a second reader renaming has_more is the drift this contract exists to stop",
            setOf("messages", "returned", "offset", "has_more"),
            messages.keys,
        )
        assertEquals(setOf("calls", "returned", "offset", "has_more"), calls.keys)
        listOf("returned", "offset", "has_more").forEach { field ->
            assertEquals(field, messages.getValue(field), calls.getValue(field))
        }
    }

    @Test
    fun `total is omitted when absent and never sent as a misleading zero`() {
        val withoutTotal = DeviceReadPage(offset = 0, count = 2).envelope("items", rows(2), render = ::render)

        assertNull("the model cannot tell a real 0 from 'not measured'", withoutTotal["total"])
    }

    @Test
    fun `a reader with a cheap exact count reports total under the contract's own name`() {
        val withTotal = DeviceReadPage(offset = 0, count = 2).envelope("items", rows(3), total = 412, render = ::render)

        assertEquals(412, withTotal.getValue("total").jsonPrimitive.int)
        assertTrue(withTotal.hasMore())
    }

    @Test
    fun `offset is echoed so a pager never has to remember what it asked for`() {
        assertEquals(25, DeviceReadPage(offset = 25, count = 5).envelope("items", rows(1), render = ::render).offset())
    }

    // --- the window: advertised bound == enforced bound -----------------------------------------

    @Test
    fun `the advertised count schema is built from the same bound pageFrom enforces`() {
        val properties = window.schemaProperties()

        assertEquals(window.maxCount, properties.getValue("count").jsonObject.getValue("maximum").jsonPrimitive.int)
        assertEquals(1, properties.getValue("count").jsonObject.getValue("minimum").jsonPrimitive.int)
        assertEquals(0, properties.getValue("offset").jsonObject.getValue("minimum").jsonPrimitive.int)
        assertEquals(setOf("count", "offset"), properties.keys)
    }

    @Test
    fun `a count above the advertised maximum clamps to it, never beyond`() {
        assertEquals(window.maxCount, window.pageFrom(buildJsonObject { put("count", 10_000) }).count)
    }

    @Test
    fun `absent args land on the default page`() {
        val page = window.pageFrom(buildJsonObject {})

        assertEquals(window.defaultCount, page.count)
        assertEquals(0, page.offset)
    }

    @Test
    fun `zero and negative args are clamped rather than refused`() {
        assertEquals(1, window.pageFrom(buildJsonObject { put("count", 0) }).count)
        assertEquals(1, window.pageFrom(buildJsonObject { put("count", -9) }).count)
        assertEquals(0, window.pageFrom(buildJsonObject { put("offset", -9) }).offset)
    }

    @Test
    fun `a non-integer count falls back to the default instead of throwing`() {
        // Args are model output; a read that refuses on a fat-fingered arg teaches the model to
        // stop paging, which loses more than the bad arg cost.
        val page = window.pageFrom(buildJsonObject { put("count", "twelve") })

        assertEquals(window.defaultCount, page.count)
    }

    @Test
    fun `an honoured count passes through untouched`() {
        val page = window.pageFrom(buildJsonObject { put("count", 7); put("offset", 21) })

        assertEquals(7, page.count)
        assertEquals(21, page.offset)
    }

    @Test
    fun `a window whose default exceeds its maximum is rejected at construction`() {
        listOf({ DeviceReadWindow(defaultCount = 80, maxCount = 50) }, { DeviceReadWindow(defaultCount = 1, maxCount = 0) })
            .forEach { construct ->
                try {
                    construct()
                    org.junit.Assert.fail("expected IllegalArgumentException")
                } catch (expected: IllegalArgumentException) {
                    // a window that advertises what it cannot honour is the defect, caught early
                }
            }
    }

    @Test
    fun `the SMS window is a page size, not the old global cap of five`() {
        assertEquals(50, DeviceReadWindow.SMS.maxCount)
        assertEquals(10, DeviceReadWindow.SMS.defaultCount)
    }
}
