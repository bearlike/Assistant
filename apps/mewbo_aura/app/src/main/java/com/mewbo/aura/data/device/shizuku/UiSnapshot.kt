package com.mewbo.aura.data.device.shizuku

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Reads the screen's element tree from inside the shell-UID service.
 *
 * **Why the XML is parsed rather than the tree walked directly.** The obvious
 * design is to hold a `UiAutomation` connection open in this process and read
 * `AccessibilityNodeInfo` straight off it — one connection, no serialization.
 * That is the right end state and it is deliberately NOT what this does yet:
 * the connection needs hidden `UiAutomationConnection` internals whose shape
 * moves between API levels, and an unverified reflection chain here fails at
 * runtime on a device rather than at compile time. Measured on the dev device,
 * a dump costs ~2.0s against a 30s dispatch budget — affordable, and honest
 * about what has actually been proven to work.
 *
 * What this does avoid is the file round-trip: the dump is written to stdout
 * and parsed in memory, so there is no `/sdcard` write, no read back, and no
 * temp file to leak. Replacing this with a held `UiAutomation` connection is a
 * change behind this one function, with the pruning and wire format unaffected.
 *
 * **The dump target must name stdout, and `/dev/tty` does not.** `/dev/tty` is
 * the controlling TERMINAL, which an interactive `adb shell` has and a
 * `ProcessBuilder("sh", "-c", …)` does not. Typed by hand the XML appears on
 * screen and the command looks correct; run from the service the tree is
 * written nowhere and stdout carries one line of chatter. Measured: every
 * element read in the app's history returned [NO_UI_TREE] until this was
 * [STDOUT_PATH]. The model's recourse was to reach for the shell and tap raw
 * pixels it extracted from `bounds`, which is exactly the coordinate drift
 * index addressing exists to remove.
 *
 * **Verifying this by hand will mislead you, so read this first.** What
 * [STDOUT_PATH] resolves to depends on what fd 1 IS, and the two cases give
 * opposite answers. Measured on the dev device, same command, same screen:
 *
 * ```
 * adb shell 'uiautomator dump /proc/self/fd/1'          ->  0 nodes  (fd 1 is a PTY)
 * adb shell 'uiautomator dump /proc/self/fd/1 | cat'    -> 47 nodes  (fd 1 is a pipe)
 * ```
 *
 * A `Process`'s `inputStream` is always a PIPE, so the second line is the case
 * this code runs in and the first is an artifact of testing through a
 * terminal. Reproduce it through a pipe, or the fix will look broken.
 */
object UiSnapshot {

    private val NODE_RE = Regex("<node ([^>]*)/?>")
    private val ATTR_RE = Regex("""(\w[\w-]*)="([^"]*)"""")
    private val BOUNDS_RE = Regex("""\[(-?\d+),(-?\d+)]\[(-?\d+),(-?\d+)]""")

    /** The command whose output is parsed. Exposed so a test can assert the
     * TARGET rather than the parse — a suite that feeds [parse] good XML stays
     * green forever while the thing producing the XML is broken, which is how
     * `/dev/tty` survived every gate. */
    val DUMP_COMMAND: String = "uiautomator dump $STDOUT_PATH"

    /**
     * Pruned, indexed element list as JSON, ready for the wire.
     *
     * The foreground app is stamped onto BOTH outcomes, including the failed
     * one: "the tree could not be read" and "the tree could not be read, and
     * you are looking at the lock screen" are different problems, and only the
     * second one tells the model what to do next.
     */
    fun readPrunedJson(exec: (String, Long) -> String): String {
        val xml = exec(DUMP_COMMAND, DUMP_TIMEOUT_MS)
        // Sampled AFTER the dump, not before: the dump settles the window
        // before serializing it, so a focus read taken afterwards names the
        // screen the tree describes rather than the one it started from. The
        // two reads are still not atomic — a screen changing mid-observation
        // can disagree, which is why the settle exists at the action layer.
        val focus = FocusedWindow.parse(exec(FocusedWindow.DUMP_COMMAND, FocusedWindow.TIMEOUT_MS))
        val body = if (!xml.contains("<node")) {
            buildJsonObject {
                put("elements", JsonArray(emptyList()))
                put("count", 0)
                put("error", NO_UI_TREE)
            }
        } else {
            ElementPruner().toWire(parse(xml))
        }
        return withForeground(body, focus).toString()
    }

    /**
     * Stamp the foreground app onto an elements payload, FIRST.
     *
     * Order is deliberate. `elements` is the long field, so keys appended
     * after it sit behind however many hundred entries the screen produced —
     * exactly the tail a result cap cuts. A one-line fact that answers "what
     * am I looking at" belongs where it survives and where it is read.
     *
     * An unparseable or app-less focus omits BOTH keys rather than emitting a
     * placeholder: absence is a state the model can act on, an invented
     * package is not.
     */
    private fun withForeground(body: JsonObject, focus: FocusedWindow?): JsonObject =
        buildJsonObject {
            focus?.let { window ->
                put("package", window.packageName)
                window.activity?.let { put("activity", it) }
            }
            body.forEach { (key, value) -> put(key, value) }
        }

    /** Parse a uiautomator XML dump into flat elements. Total by construction:
     * model-facing input is a black box, so a malformed node is skipped, never
     * thrown. */
    fun parse(xml: String): List<ScreenElement> =
        NODE_RE.findAll(xml).mapNotNull { match ->
            val attrs = ATTR_RE.findAll(match.groupValues[1])
                .associate { it.groupValues[1] to it.groupValues[2] }
            val bounds = BOUNDS_RE.find(attrs["bounds"].orEmpty()) ?: return@mapNotNull null
            ScreenElement(
                index = 0, // assigned by the pruner, after filtering
                text = attrs["text"]?.unescapeXml(),
                contentDescription = attrs["content-desc"]?.unescapeXml(),
                hint = attrs["hint"]?.unescapeXml(),
                resourceId = attrs["resource-id"]?.substringAfterLast('/'),
                className = attrs["class"]?.substringAfterLast('.'),
                packageName = attrs["package"],
                clickable = attrs["clickable"] == "true",
                scrollable = attrs["scrollable"] == "true",
                editable = attrs["class"]?.contains("EditText") == true,
                checkable = attrs["checkable"] == "true",
                left = bounds.groupValues[1].toInt(),
                top = bounds.groupValues[2].toInt(),
                right = bounds.groupValues[3].toInt(),
                bottom = bounds.groupValues[4].toInt(),
            )
        }.toList()

    private fun String.unescapeXml(): String = this
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")

    /** `uiautomator dump` takes a FILE, so stdout has to be named as one.
     * `/proc/self/fd/1` is that name and needs no controlling terminal, which
     * is the whole difference from `/dev/tty`. */
    const val STDOUT_PATH = "/proc/self/fd/1"

    /** Returned when the dump produced no tree. Named so the test asserting
     * its ABSENCE reads as the contract it is. */
    const val NO_UI_TREE = "no_ui_tree"

    private const val DUMP_TIMEOUT_MS = 10_000L
}
