package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.ElementPruner
import com.mewbo.aura.data.device.shizuku.ScreenElement
import com.mewbo.aura.data.device.shizuku.SettlePolicy
import com.mewbo.aura.data.device.shizuku.ShizukuDeviceControl
import com.mewbo.aura.data.device.shizuku.UiSnapshot
import javax.inject.Inject
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/**
 * `device_action` — the acting half. Six actions behind one `action` enum.
 *
 * Two properties do most of the work here:
 *
 * - **Targets are element INDEXES, never coordinates.** This class resolves an
 *   index to a centre point locally, so the model never does pixel arithmetic.
 *   The alternative is the well-documented failure where coordinates computed
 *   against a downscaled image are applied to full-resolution device space:
 *   every tap lands proportionally wrong, hitting a real control rather than
 *   erroring, so nothing reports a problem.
 * - **The result carries the NEXT observation.** After acting, this settles and
 *   returns the fresh element list inside the action's own result. That halves
 *   the round-trip count, which matters here more than in any adjacent-loop
 *   harness: each of our round trips is a network hop plus an SSE delivery plus
 *   an HTTP POST back, all inside a 30s budget.
 */
class DeviceActionHandler @Inject constructor(
    private val control: ShizukuDeviceControl,
    private val veil: ScreenCaptureVeil,
) : DeviceToolHandler {
    override val toolId: String = "device_action"

    private val settlePolicy = SettlePolicy()

    override suspend fun execute(args: JsonObject): JsonObject {
        val service = control.service() ?: throw DeviceControlUnavailableException()
        val action = args.optArgString("action")
            ?: throw DeviceToolArgsException("Missing required arg 'action'.")

        val performed = when (action) {
            // **Veiled, and not for the screenshot reason.** An injected touch is delivered to the
            // topmost window at those coordinates, and the grant's own Stop pill is a window near
            // the bottom centre — so a tap aimed at a button underneath it would press Stop and
            // end the very grant it is acting under. A window that is not on screen cannot be
            // hit. `type` is in this set because it TAPS to focus the field first; only `key`,
            // `launch` and `wait` never go through the screen and so pay no hide/restore.
            "tap" -> veil.hiddenDuring { tap(service, args) }
            "swipe" -> veil.hiddenDuring { swipe(service, args) }
            "type" -> veil.hiddenDuring { type(service, args) }
            "key" -> service.pressKey(
                args.optArgString("key")
                    ?: throw DeviceToolArgsException("action='key' needs 'key'."),
            )
            "launch" -> service.launch(
                args.optArgString("package_name")
                    ?: throw DeviceToolArgsException("action='launch' needs 'package_name'."),
            )
            "wait" -> true
            else -> throw DeviceToolArgsException(
                "Unknown action '$action' — expected tap, swipe, type, key, launch or wait.",
            )
        }

        val settled = settlePolicy.settle(
            now = { System.currentTimeMillis() },
            sleep = { delay(it) },
            read = { service.elements() },
        )
        val screen = runCatching {
            kotlinx.serialization.json.Json.parseToJsonElement(settled.value).jsonObject
        }.getOrNull()

        return buildJsonObject {
            put("performed", performed)
            // Reported because a screen still moving at the cap is a real
            // condition the model should account for, not a silent one.
            put("settled", settled.settled)
            screen?.forEach { (key, value) -> put(key, value) }
        }
    }

    private suspend fun tap(
        service: com.mewbo.aura.data.device.shizuku.IDeviceService,
        args: JsonObject,
    ): Boolean {
        val element = resolve(service, args)
        return service.tap(element.centerX, element.centerY)
    }

    private suspend fun type(
        service: com.mewbo.aura.data.device.shizuku.IDeviceService,
        args: JsonObject,
    ): Boolean {
        val text = args.optArgString("text")
            ?: throw DeviceToolArgsException("action='type' needs 'text'.")
        // Focus the field first when an index is given; typing into whatever
        // happens to hold focus is how text lands in the wrong box.
        args.optArgInt("index")?.let {
            val element = resolve(service, args)
            service.tap(element.centerX, element.centerY)
        }
        return service.typeText(text)
    }

    private suspend fun swipe(
        service: com.mewbo.aura.data.device.shizuku.IDeviceService,
        args: JsonObject,
    ): Boolean {
        val geometry = control.geometry()
            ?: throw DeviceControlUnavailableException()
        val midX = geometry.width / 2
        val midY = geometry.height / 2
        val dx = geometry.width / 4
        val dy = geometry.height / 4
        // A swipe UP scrolls the content DOWN — the gesture is named for the
        // finger, which is what the model means by it.
        return when (args.optArgString("direction") ?: "up") {
            "up" -> service.swipe(midX, midY + dy, midX, midY - dy, SWIPE_MS)
            "down" -> service.swipe(midX, midY - dy, midX, midY + dy, SWIPE_MS)
            "left" -> service.swipe(midX + dx, midY, midX - dx, midY, SWIPE_MS)
            "right" -> service.swipe(midX - dx, midY, midX + dx, midY, SWIPE_MS)
            else -> throw DeviceToolArgsException(
                "'direction' must be up, down, left or right.",
            )
        }
    }

    /**
     * Index → element, re-read at action time.
     *
     * **A stale index is a structured error, never a silent mis-tap.** The
     * element list the model is holding may be several turns old and the screen
     * may have moved under it; tapping index 7 of a list that no longer has one
     * would hit whatever is there now. Refusing hands the model something it
     * can recover from — re-observe and retry.
     */
    private fun resolve(
        service: com.mewbo.aura.data.device.shizuku.IDeviceService,
        args: JsonObject,
    ): ScreenElement {
        val index = args.optArgInt("index")
            ?: throw DeviceToolArgsException("This action needs an element 'index'.")
        val elements = ElementPruner().prune(UiSnapshot.parse(service.shell(DUMP_CMD, DUMP_TIMEOUT)))
        return elements.getOrNull(index) ?: throw DeviceToolArgsException(
            "No element with index $index on the current screen (${elements.size} available). " +
                "The screen changed — observe again with device_ui and retry.",
        )
    }

    private companion object {
        const val SWIPE_MS = 300
        const val DUMP_CMD = "uiautomator dump /dev/tty"
        const val DUMP_TIMEOUT = 10_000
    }
}
