package com.mewbo.aura.data.device.shizuku

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * One addressable element on screen.
 *
 * [index] is the ONLY thing the model ever names. The bounds stay here, on the
 * client, and are resolved to a centre point locally — so the model never does
 * pixel arithmetic and the classic scaling defect (coordinates computed against
 * a downscaled screenshot, applied to full-resolution device space, landing on
 * the wrong control while erroring nowhere) cannot occur.
 */
data class ScreenElement(
    val index: Int,
    val text: String? = null,
    val contentDescription: String? = null,
    val hint: String? = null,
    val resourceId: String? = null,
    val className: String? = null,
    val packageName: String? = null,
    val clickable: Boolean = false,
    val scrollable: Boolean = false,
    val editable: Boolean = false,
    val checkable: Boolean = false,
    val left: Int = 0,
    val top: Int = 0,
    val right: Int = 0,
    val bottom: Int = 0,
) {
    val centerX: Int get() = (left + right) / 2
    val centerY: Int get() = (top + bottom) / 2

    /**
     * The wire shape — **deliberately without geometry.**
     *
     * Index addressing means the client owns index→centre resolution, so four
     * coordinates per element would be paid for on every observation and read
     * by nobody. Dropping them is the single largest context saving available
     * here, and it is structurally unavailable to a coordinate-addressed
     * design. Empty fields are omitted for the same reason.
     */
    fun toWireEntry(): JsonObject = buildJsonObject {
        put("i", index)
        text?.takeIf { it.isNotBlank() }?.let { put("text", it) }
        contentDescription?.takeIf { it.isNotBlank() }?.let { put("desc", it) }
        hint?.takeIf { it.isNotBlank() }?.let { put("hint", it) }
        resourceId?.takeIf { it.isNotBlank() }?.let { put("id", it) }
        className?.takeIf { it.isNotBlank() }?.let { put("cls", it) }
        if (clickable) put("clickable", true)
        if (scrollable) put("scrollable", true)
        if (editable) put("editable", true)
        if (checkable) put("checkable", true)
    }
}

/**
 * Decides which nodes of a screen are worth showing the model, and numbers them.
 *
 * Pure logic over a list — no device, no Android types — which is what makes
 * the rule testable on the JVM. A raw node tree of a dense screen is mostly
 * layout scaffolding: routinely 50-200 KB of it, a large fraction of a context
 * window spent on containers the model can neither read nor tap.
 */
class ElementPruner(private val maxElements: Int = DEFAULT_MAX_ELEMENTS) {

    /**
     * Keep a node iff it carries something the model can READ or ACT on, and
     * occupies real space.
     *
     * The zero-area test is not a tidiness filter: an off-screen or collapsed
     * node is tappable in the tree and hits nothing on the glass, so keeping it
     * offers the model a target that silently does nothing.
     */
    fun prune(nodes: List<ScreenElement>): List<ScreenElement> =
        nodes.asSequence()
            .filter { it.isMeaningful() && it.hasArea() }
            .take(maxElements)
            .mapIndexed { index, element -> element.copy(index = index) }
            .toList()

    /**
     * The wire payload: the pruned list, plus an honest note when it was cut.
     *
     * A silently truncated list reads to the model as the whole screen, so it
     * concludes an element is absent when it was merely dropped. Saying so
     * costs a few tokens and is the difference between "not there" and "not
     * shown".
     */
    fun toWire(nodes: List<ScreenElement>): JsonObject {
        val kept = prune(nodes)
        val eligible = nodes.count { it.isMeaningful() && it.hasArea() }
        return buildJsonObject {
            put("elements", JsonArray(kept.map { it.toWireEntry() }))
            put("count", kept.size)
            if (eligible > kept.size) {
                put("truncated", true)
                put("total_matching", eligible)
            }
        }
    }

    private fun ScreenElement.isMeaningful(): Boolean =
        !text.isNullOrBlank() ||
            !contentDescription.isNullOrBlank() ||
            !hint.isNullOrBlank() ||
            !resourceId.isNullOrBlank() ||
            checkable

    private fun ScreenElement.hasArea(): Boolean = right > left && bottom > top

    companion object {
        /** Bounds one observation. No reference project caps this; an uncapped
         * dense screen is what makes a tree dump unaffordable. */
        const val DEFAULT_MAX_ELEMENTS = 120
    }
}
