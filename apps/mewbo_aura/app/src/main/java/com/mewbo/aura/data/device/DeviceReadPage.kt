package com.mewbo.aura.data.device

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * The bounds ONE device read both ADVERTISES and ENFORCES - held in a single object so the two
 * cannot drift apart.
 *
 * A schema promising `maximum: 50` over a handler clamping to 5 is a silent narrowing: the model
 * asks for what it was told it could have and quietly receives less, which is the same disease as
 * an unmarked truncated page one layer up. [schemaProperties] and [pageFrom] read the same two
 * fields of the same instance, so the only way to change one is to change both.
 *
 * Every `device_*` READ is expected to declare a window here rather than hand-writing `count`/
 * `offset` into its own schema - a second tool inventing `limit`/`start` costs the model a second
 * contract to learn for no gain.
 */
data class DeviceReadWindow(val defaultCount: Int, val maxCount: Int) {
    init {
        require(maxCount >= 1) { "maxCount must be at least 1: $maxCount" }
        require(defaultCount in 1..maxCount) { "defaultCount must be in 1..$maxCount: $defaultCount" }
    }

    /** The `count`/`offset` half of a read tool's `parameters.properties`, to be merged with
     * whatever narrowing arguments the tool adds of its own (SMS adds `sender_filter`). */
    fun schemaProperties(): JsonObject = buildJsonObject {
        put("count", DeviceToolCatalog.integerSchema(minimum = 1, maximum = maxCount))
        put("offset", DeviceToolCatalog.integerSchema(minimum = 0))
    }

    /**
     * Resolve one call's page out of raw model args. TOTAL by construction - a missing, negative,
     * oversized or non-integer `count`/`offset` lands on a usable page rather than an error,
     * because args are model output and a read that refuses on a fat-fingered offset teaches the
     * model to stop paging.
     */
    fun pageFrom(args: JsonObject): DeviceReadPage = DeviceReadPage(
        offset = (args.optArgInt("offset") ?: 0).coerceAtLeast(0),
        count = (args.optArgInt("count") ?: defaultCount).coerceIn(1, maxCount),
    )

    companion object {
        /** 50 message bodies is a few KB of tool result; 10 answers "what did I miss" in one call. */
        val SMS = DeviceReadWindow(defaultCount = 10, maxCount = 50)
    }
}

/**
 * One page of a device read, and the reason this class exists at all: **a device read must
 * distinguish "that is everything" from "that is the first N", and no reader may invent its own
 * way of saying so.**
 *
 * Measured, from a real session: a read that returned a well-formed page with no truncation field
 * produced a confident answer built on a fraction of the data, and nothing anywhere - not the
 * response, not a log, not a test - marked it partial. The same session found that the contacts
 * that mattered were in a table the agent could not reach at all, so a reader being *complete* is
 * never something the harness can assume. A tool that errors is diagnosable; a tool that silently
 * narrows is not.
 *
 * The envelope is therefore fixed for every reader: `returned`, `offset`, `has_more`, and
 * optionally `total`. Adopt it verbatim - `has_more` under a different name on the call log is
 * exactly the per-capability drift that produced three inconsistent surfaces.
 *
 * Cost: `O(count)`, bounded by the window. Deriving `has_more` costs ONE extra row
 * ([fetchLimit]), never a second count query over the whole table - which is why `total` is
 * optional rather than required. A reader that cannot produce an exact total cheaply must still
 * be able to report truncation honestly.
 */
data class DeviceReadPage(val offset: Int, val count: Int) {

    /** What the underlying reader must be asked for: the page plus ONE look-ahead row. Its only
     * job is to answer "is there more" - [envelope] trims it, and because the trim happens before
     * the render lambda runs, a look-ahead row cannot leak into the response even by mistake. */
    val fetchLimit: Int get() = count + 1

    /**
     * Build the response from the [fetchLimit]-sized [fetched] list. [itemsKey] names the payload
     * for the model (`messages`, `calls`, `contacts`); the three envelope fields around it never
     * change.
     *
     * [total] is for a reader whose table makes an exact count cheap. Omitted when null rather
     * than sent as 0 - a wrong total is worse than an absent one, since the model has no way to
     * tell a real 0 from "not measured".
     */
    fun <T> envelope(
        itemsKey: String,
        fetched: List<T>,
        total: Int? = null,
        render: (T) -> JsonObject,
    ): JsonObject {
        val items = fetched.take(count).map(render)
        return buildJsonObject {
            put(itemsKey, JsonArray(items))
            put("returned", items.size)
            put("offset", offset)
            put("has_more", fetched.size > count)
            if (total != null) put("total", total)
        }
    }
}
