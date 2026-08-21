package com.mewbo.aura.data.device.shizuku

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The device-control logic that needs no device: geometry parsing, element
 * pruning, and the settle bound.
 *
 * Keeping this surface pure is the design, not a convenience — the
 * privilege-dependent half (binder, injection, capture) is deliberately thin
 * precisely so almost everything can be pinned here.
 */
class DisplayGeometryTest {

    /** The dev container reports exactly this, so it is a real fixture. */
    private val redroidSize = "Physical size: 1440x3120"
    private val redroidDensity = "Physical density: 560"

    @Test
    fun `parses the physical size and density`() {
        val geometry = DisplayGeometry.parse(redroidSize, redroidDensity)
        assertEquals(DisplayGeometry(1440, 3120, 560), geometry)
    }

    @Test
    fun `an override size WINS over the physical one`() {
        // The case that would silently corrupt the coordinate space: the panel
        // is 1440 wide, the window manager addresses 1080, and every tap lands
        // proportionally wrong while erroring nowhere.
        val output = "Physical size: 1440x3120\nOverride size: 1080x2340"
        assertEquals(1080 to 2340, DisplayGeometry.parseSize(output))
    }

    @Test
    fun `an override density WINS over the physical one`() {
        val output = "Physical density: 560\nOverride density: 420"
        assertEquals(420, DisplayGeometry.parseDensity(output))
    }

    @Test
    fun `unparseable output yields null rather than a wrong geometry`() {
        // A guessed geometry is worse than none: it would be used for index to
        // centre resolution and put every tap in the wrong place.
        assertNull(DisplayGeometry.parse("error: unknown command", ""))
        assertNull(DisplayGeometry.parseSize(""))
    }
}

class ElementPrunerTest {

    private fun node(
        text: String? = null,
        desc: String? = null,
        resourceId: String? = null,
        checkable: Boolean = false,
        left: Int = 0,
        top: Int = 0,
        right: Int = 100,
        bottom: Int = 50,
    ) = ScreenElement(
        index = 0,
        text = text,
        contentDescription = desc,
        resourceId = resourceId,
        checkable = checkable,
        left = left,
        top = top,
        right = right,
        bottom = bottom,
    )

    @Test
    fun `keeps a node carrying text, description, resource id or checkability`() {
        val kept = ElementPruner().prune(
            listOf(
                node(text = "Save"),
                node(desc = "Back"),
                node(resourceId = "com.x:id/ok"),
                node(checkable = true),
            ),
        )
        assertEquals(4, kept.size)
    }

    @Test
    fun `drops pure layout scaffolding the model can neither read nor tap`() {
        val kept = ElementPruner().prune(listOf(node(), node(), node(text = "Real")))
        assertEquals(1, kept.size)
        assertEquals("Real", kept.single().text)
    }

    @Test
    fun `drops a zero-area node even when it carries text`() {
        // Tappable in the tree, hits nothing on the glass — keeping it offers
        // the model a target that silently does nothing.
        val kept = ElementPruner().prune(
            listOf(node(text = "Collapsed", right = 0, bottom = 0), node(text = "Visible")),
        )
        assertEquals(listOf("Visible"), kept.map { it.text })
    }

    @Test
    fun `indexes are contiguous from zero AFTER filtering`() {
        // The model addresses by index, so a gap left by a dropped node would
        // make every later index point at the wrong element.
        val kept = ElementPruner().prune(
            listOf(node(), node(text = "A"), node(), node(text = "B")),
        )
        assertEquals(listOf(0, 1), kept.map { it.index })
        assertEquals(listOf("A", "B"), kept.map { it.text })
    }

    @Test
    fun `caps the element count and SAYS SO on the wire`() {
        // A silently truncated list reads as the whole screen, so the model
        // concludes an element is absent when it was merely not shown.
        val nodes = (1..200).map { node(text = "item $it") }
        val wire = ElementPruner(maxElements = 10).toWire(nodes).toString()

        assertTrue(wire.contains("\"truncated\":true"))
        assertTrue(wire.contains("\"total_matching\":200"))
        assertTrue(wire.contains("\"count\":10"))
    }

    @Test
    fun `an untruncated list carries no truncation claim`() {
        val wire = ElementPruner().toWire(listOf(node(text = "only"))).toString()
        assertFalse(wire.contains("truncated"))
    }

    @Test
    fun `geometry NEVER crosses the wire`() {
        // Index addressing means the client resolves index to centre locally.
        // Four coordinates per element would be paid for on every observation
        // and read by nobody — the largest single context saving here.
        val wire = ElementPruner().toWire(
            listOf(node(text = "Save", left = 10, top = 20, right = 300, bottom = 90)),
        ).toString()

        assertFalse(wire.contains("bounds"))
        assertFalse(wire.contains("\"left\""))
        assertFalse(wire.contains("300"))
        assertTrue(wire.contains("Save"))
    }

    @Test
    fun `the centre a tap resolves to is the middle of the element`() {
        val element = node(left = 100, top = 200, right = 300, bottom = 400)
        assertEquals(200, element.centerX)
        assertEquals(300, element.centerY)
    }
}

class UiSnapshotParseTest {

    @Test
    fun `parses attributes and bounds out of a uiautomator dump`() {
        val xml = """
            <hierarchy rotation="0">
            <node index="0" text="Settings" resource-id="com.android.settings:id/title"
             class="android.widget.TextView" package="com.android.settings" content-desc=""
             checkable="false" clickable="true" scrollable="false" bounds="[42,100][400,180]" />
            </hierarchy>
        """.trimIndent()

        val parsed = UiSnapshot.parse(xml)

        assertEquals(1, parsed.size)
        val node = parsed.single()
        assertEquals("Settings", node.text)
        assertEquals("title", node.resourceId) // shortened past the package prefix
        assertEquals("TextView", node.className)
        assertTrue(node.clickable)
        assertEquals(42, node.left)
        assertEquals(180, node.bottom)
    }

    @Test
    fun `unescapes XML entities in user-visible text`() {
        val xml = """<node text="Tom &amp; Jerry &lt;3" bounds="[0,0][10,10]" />"""
        assertEquals("Tom & Jerry <3", UiSnapshot.parse(xml).single().text)
    }

    @Test
    fun `a malformed node is skipped, never thrown`() {
        // Parsing input is total by construction — a dump that changed shape
        // must degrade to fewer elements, not take the tool call down.
        val xml = """
            <node text="no bounds here" />
            <node text="fine" bounds="[0,0][10,10]" />
        """.trimIndent()
        assertEquals(listOf("fine"), UiSnapshot.parse(xml).map { it.text })
    }

    @Test
    fun `an empty dump yields no elements`() {
        assertEquals(emptyList<ScreenElement>(), UiSnapshot.parse(""))
    }
}

/**
 * The dump COMMAND, which is a separate contract from the parse.
 *
 * Every test above feeds [UiSnapshot.parse] good XML and passes — and did so
 * for the entire life of a build in which no element read ever succeeded,
 * because the command that produces the XML named `/dev/tty`. That target is
 * the controlling TERMINAL: an interactive shell has one, so the dump looks
 * right when typed by hand, and `ProcessBuilder("sh", "-c", …)` has none, so
 * from the service the tree went nowhere.
 *
 * A test that only exercises the parser cannot see that. These assert on what
 * is EXECUTED.
 */
class UiSnapshotCommandTest {

    @Test
    fun `the dump target is stdout, never the controlling terminal`() {
        assertFalse(
            "/dev/tty needs a controlling terminal the service does not have",
            UiSnapshot.DUMP_COMMAND.contains("/dev/tty"),
        )
        assertTrue(UiSnapshot.DUMP_COMMAND.contains(UiSnapshot.STDOUT_PATH))
    }

    @Test
    fun `readPrunedJson runs exactly the two declared commands`() {
        // Pins both constants to the call, so changing one without the other
        // cannot pass. The exec double returns a real dump.
        val executed = mutableListOf<String>()
        val xml = """<node text="Save" bounds="[0,0][100,50]" />"""

        UiSnapshot.readPrunedJson { command, _ -> executed += command; xml }

        assertEquals(listOf(UiSnapshot.DUMP_COMMAND, FocusedWindow.DUMP_COMMAND), executed)
    }

    @Test
    fun `an observation costs exactly two commands, not three`() {
        // The element read is the cost centre of this whole surface (~2.0s
        // measured), and the focus read is ~0.01s of that. A third command
        // added without a measurement is how that ratio quietly stops holding.
        var calls = 0
        UiSnapshot.readPrunedJson { _, _ -> calls++; "" }
        assertEquals(2, calls)
    }

    @Test
    fun `a dump carrying nodes produces elements, not an error`() {
        val xml = """
            <hierarchy rotation="0">
            <node text="Save" clickable="true" bounds="[0,0][100,50]" />
            </hierarchy>
        """.trimIndent()

        val wire = UiSnapshot.readPrunedJson { _, _ -> xml }

        assertFalse(wire.contains(UiSnapshot.NO_UI_TREE))
        assertTrue(wire.contains("Save"))
    }

    @Test
    fun `the chatter uiautomator prints when it writes elsewhere is NOT a tree`() {
        // The exact stdout `uiautomator dump /dev/tty` produced from the
        // service: a success message, and no tree. Reported as an error rather
        // than as an empty screen, because an empty list reads as "nothing is
        // on screen" and sends the model looking for a UI problem.
        val chatter = "UI hierchary dumped to: /dev/tty"

        val wire = UiSnapshot.readPrunedJson { _, _ -> chatter }

        assertTrue(wire.contains(UiSnapshot.NO_UI_TREE))
    }
}

/**
 * The foreground app, which is a FIELD on the observation and not a tool.
 *
 * Every device task in the event history opened by asking what app was on
 * screen, and with no answer available the model ran `dumpsys window | grep
 * mCurrentFocus` by hand — five times across two sessions. These pin the parse
 * and the refusal to guess.
 */
class FocusedWindowTest {

    /** Exactly what the dev device prints. */
    private val settings =
        "  mCurrentFocus=Window{de6f9ad u0 com.android.settings/com.android.settings.Settings}"

    @Test
    fun `parses the package and the fully qualified activity`() {
        val focus = FocusedWindow.parse(settings)
        assertEquals("com.android.settings", focus?.packageName)
        assertEquals("com.android.settings.Settings", focus?.activity)
    }

    @Test
    fun `a relative class name is expanded against its package`() {
        // So the value can be handed straight back to `am start -n`, which is
        // what the model does with it.
        val focus = FocusedWindow.parse("mCurrentFocus=Window{a1 u0 com.example/.MainActivity}")
        assertEquals("com.example.MainActivity", focus?.activity)
    }

    @Test
    fun `nothing focused yields null rather than a guess`() {
        assertNull(FocusedWindow.parse("mCurrentFocus=null"))
        assertNull(FocusedWindow.parse(""))
    }

    @Test
    fun `a bare system window names no app, so it reports none`() {
        // `StatusBar` is a WINDOW name, not a package. Reporting it as one
        // would send the model launching and tapping against a package that
        // does not exist.
        assertNull(FocusedWindow.parse("mCurrentFocus=Window{a1 u0 StatusBar}"))
        assertNull(FocusedWindow.parse("mCurrentFocus=Window{a1 u0 NavigationBar0}"))
    }

    @Test
    fun `the focus command asks the window manager, not the activity manager`() {
        // Pinned because it is the command the model itself converged on, and
        // because the two dumpsys sections that look interchangeable are not:
        // `dumpsys activity activities | grep mResumedActivity` returns
        // nothing on the dev device.
        assertTrue(FocusedWindow.DUMP_COMMAND.contains("dumpsys window"))
        assertTrue(FocusedWindow.DUMP_COMMAND.contains("mCurrentFocus"))
    }
}

/** The foreground app as it reaches the wire, on BOTH observation outcomes. */
class ForegroundOnTheWireTest {

    private val focusOutput =
        "mCurrentFocus=Window{de6f9ad u0 com.android.settings/com.android.settings.Settings}"

    private fun wireFor(dump: String, focus: String = focusOutput): String =
        UiSnapshot.readPrunedJson { command, _ ->
            if (command == FocusedWindow.DUMP_COMMAND) focus else dump
        }

    @Test
    fun `an element list carries the package and activity`() {
        val wire = wireFor("""<node text="Save" bounds="[0,0][100,50]" />""")

        assertTrue(wire.contains("\"package\":\"com.android.settings\""))
        assertTrue(wire.contains("\"activity\":\"com.android.settings.Settings\""))
        assertTrue(wire.contains("Save"))
    }

    @Test
    fun `a FAILED tree read still says which app you are looking at`() {
        // "The tree could not be read" and "the tree could not be read, and
        // you are on the lock screen" are different problems; only the second
        // tells the model what to do next.
        val wire = wireFor("UI hierchary dumped to: /dev/tty")

        assertTrue(wire.contains(UiSnapshot.NO_UI_TREE))
        assertTrue(wire.contains("\"package\":\"com.android.settings\""))
    }

    @Test
    fun `the app is stated ONCE, not once per element`() {
        // The whole reason this is a top-level field: a package repeated on
        // every node is per-node cost for a per-screen truth.
        val dump = (1..5).joinToString("\n") {
            """<node text="row $it" package="com.android.settings" bounds="[0,0][100,50]" />"""
        }

        val wire = wireFor(dump)

        assertEquals(1, wire.split("\"package\"").size - 1)
    }

    @Test
    fun `it is stamped BEFORE the elements array`() {
        // `elements` is the long field, so a key appended after it sits behind
        // however many hundred entries the screen produced — exactly the tail
        // a result cap cuts.
        val wire = wireFor("""<node text="Save" bounds="[0,0][100,50]" />""")

        assertTrue(wire.indexOf("\"package\"") < wire.indexOf("\"elements\""))
    }

    @Test
    fun `an unreadable focus omits both keys rather than inventing one`() {
        val wire = wireFor("""<node text="Save" bounds="[0,0][100,50]" />""", focus = "")

        assertFalse(wire.contains("\"package\""))
        assertFalse(wire.contains("\"activity\""))
        assertTrue(wire.contains("Save"))
    }

    @Test
    fun `geometry STILL never crosses the wire`() {
        // Re-asserted through the full read path, not just the pruner: this is
        // the field a future edit adds back "for completeness", and the new
        // top-level keys are exactly the kind of edit that invites it.
        val wire = wireFor("""<node text="Save" bounds="[42,100][400,180]" />""")

        assertFalse(wire.contains("bounds"))
        assertFalse(wire.contains("\"left\""))
        assertFalse(wire.contains("400"))
    }
}

/**
 * A failed capture has to say WHY.
 *
 * The measured defect: `screencap` refusing a protected window still creates
 * the output file — empty — so the existence check passed, the decode returned
 * null, and the method returned `""`. The agent went blind with no diagnostic,
 * on a path one `FLAG_SECURE` window reaches.
 */
class ScreenCaptureFailureTest {

    @Test
    fun `a protected window is named as protected content, with a way out`() {
        val reason = ScreenCapture.explain("screencap: FB is protected: PERMISSION_DENIED")

        assertTrue(reason.contains("PERMISSION_DENIED"))
        assertTrue(reason.contains("protected content"))
        assertTrue(reason.contains("elements")) // the recovery that still works
    }

    @Test
    fun `the protected match survives a reworded message`() {
        // Matched as a case-insensitive substring because the wording differs
        // by Android version, and a phrasing change must not drop this to the
        // generic arm.
        assertTrue(ScreenCapture.explain("Permission Denied").contains("protected content"))
    }

    @Test
    fun `an unrecognised device message is relayed verbatim`() {
        // Whatever the device said beats anything invented here.
        val reason = ScreenCapture.explain("screencap: failed to open display")
        assertTrue(reason.contains("screencap: failed to open display"))
    }

    @Test
    fun `silence is reported as silence, never as success`() {
        val reason = ScreenCapture.explain("")

        assertTrue(reason.contains("reported no reason"))
        assertTrue(reason.isNotBlank())
    }

    @Test
    fun `every explanation names a next action`() {
        // The message is read by a model that will act on it and relayed to a
        // person holding the phone; a cause with no cure leaves both stuck.
        listOf("FB is protected: PERMISSION_DENIED", "some other error", "").forEach { output ->
            assertTrue(output, ScreenCapture.explain(output).contains("Ask the user"))
        }
    }
}

/** The binder envelope each capture outcome renders itself as. */
class ScreenCaptureWireTest {

    @Test
    fun `a capture carries the two keys the server lifts the image out of`() {
        // Renaming either one silently sends a screenshot to the model as
        // base64 TEXT — ~40x the tokens of an image block, and unreadable.
        val wire = ScreenCaptureResult.Captured("QUJD").toWire().toString()

        assertTrue(wire.contains("\"image_base64\":\"QUJD\""))
        assertTrue(wire.contains("\"image_media_type\":\"image/jpeg\""))
    }

    @Test
    fun `a failure renders the shared error envelope, not a successful apology`() {
        // The loop only reclassifies a step as failed when it sees this exact
        // shape; anything else round-trips as success=true.
        val wire = ScreenCaptureResult.Failed("nope").toWire().toString()

        assertTrue(wire.contains("\"error\""))
        assertTrue(wire.contains("\"code\":\"capture_failed\""))
        assertTrue(wire.contains("\"message\":\"nope\""))
        assertFalse(wire.contains("image_base64"))
    }

    @Test
    fun `a capture survives the binder round trip`() {
        // Through the REAL writer, not a hand-built fixture: a test that
        // asserts a fixture against itself stays green while the two halves
        // drift apart.
        val back = ScreenCaptureResult.fromWire(ScreenCaptureResult.Captured("QUJD").toWire().toString())
        assertEquals(ScreenCaptureResult.Captured("QUJD"), back)
    }

    @Test
    fun `a failure survives the round trip with its reason intact`() {
        // The reason IS the feature — losing it on the way back would restore
        // exactly the blindness this change removes.
        val reason = ScreenCapture.explain("FB is protected: PERMISSION_DENIED")
        val back = ScreenCaptureResult.fromWire(ScreenCaptureResult.Failed(reason).toWire().toString())
        assertEquals(ScreenCaptureResult.Failed(reason), back)
    }

    @Test
    fun `junk from the other side is a Failed, never an exception`() {
        // This crosses a process boundary, so "the other side sent something
        // unexpected" is a state to report, not a crash that takes the tool
        // call down.
        listOf("", "not json", "[]", "{}", """{"image_base64":""}""", """{"error":{}}""")
            .forEach { input ->
                val back = ScreenCaptureResult.fromWire(input)
                assertTrue(input, back is ScreenCaptureResult.Failed)
                assertTrue(input, (back as ScreenCaptureResult.Failed).reason.contains("Ask the user"))
            }
    }

    @Test
    fun `a nested value where a string was expected does not throw`() {
        // The `jsonPrimitive` accessor THROWS on an object or array; `as?`
        // does not. Model-adjacent input crossing a binder is a black box.
        val back = ScreenCaptureResult.fromWire("""{"error":{"message":{"nested":1}}}""")
        assertTrue(back is ScreenCaptureResult.Failed)
    }
}

class SettlePolicyTest {

    /** A virtual clock — the point of injecting it is that no test sleeps. */
    private class FakeClock {
        var now = 0L
        val slept = mutableListOf<Long>()
        suspend fun sleep(ms: Long) {
            slept += ms
            now += ms
        }
    }

    @Test
    fun `settles once the screen reads identically three times`() = runTest {
        val clock = FakeClock()
        val policy = SettlePolicy()

        val result = policy.settle(now = { clock.now }, sleep = clock::sleep) { "stable" }

        assertTrue(result.settled)
        assertEquals("stable", result.value)
        // Three reads => two sleeps, not one per poll to the timeout.
        assertEquals(2, clock.slept.size)
    }

    @Test
    fun `a never-idle screen is CAPPED rather than polled forever`() = runTest {
        // The uiautomator2 lesson: a device that never idles (animation, video,
        // a blinking cursor, an ad) hangs an unbounded waiter. Inside a 30s
        // dispatch budget that is a timeout generator, so the cap is the whole
        // safety property.
        val clock = FakeClock()
        var counter = 0
        val policy = SettlePolicy()

        val result = policy.settle(now = { clock.now }, sleep = clock::sleep) { "frame ${counter++}" }

        assertFalse(result.settled)
        assertTrue("must stop at the cap", clock.now <= SettlePolicy.DEFAULT_TIMEOUT_MS + 500)
    }

    @Test
    fun `the cap leaves most of the 30s dispatch budget for the round trip`() {
        assertEquals(6_000L, SettlePolicy.DEFAULT_TIMEOUT_MS)
        assertEquals(3, SettlePolicy.DEFAULT_STABLE_READS)
        assertEquals(500L, SettlePolicy.DEFAULT_POLL_INTERVAL_MS)
    }

    @Test
    fun `a screen that stabilises LATE still settles`() = runTest {
        val clock = FakeClock()
        var reads = 0
        val policy = SettlePolicy()

        val result = policy.settle(now = { clock.now }, sleep = clock::sleep) {
            reads++
            if (reads < 3) "loading $reads" else "done"
        }

        assertTrue(result.settled)
        assertEquals("done", result.value)
    }
}
