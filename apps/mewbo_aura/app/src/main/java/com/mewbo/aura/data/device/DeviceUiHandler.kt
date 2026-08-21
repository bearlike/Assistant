package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.ScreenCapture
import com.mewbo.aura.data.device.shizuku.ScreenCaptureResult
import com.mewbo.aura.data.device.shizuku.ShizukuDeviceControl
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/**
 * `device_ui` — the observation half. Two actions behind one schema:
 * `elements` (cheap, the default) and `screenshot` (an image).
 *
 * The split is the point: the element list is a few hundred tokens and answers
 * most questions, while an image costs roughly what the whole tool surface
 * costs per call. Every screenshot-only harness pays the image on every step
 * and has no way not to; this one can skip it.
 */
class DeviceUiHandler @Inject constructor(
    private val control: ShizukuDeviceControl,
    private val veil: ScreenCaptureVeil,
) : DeviceToolHandler {
    override val toolId: String = "device_ui"

    override suspend fun execute(args: JsonObject): JsonObject {
        val service = control.service() ?: throw DeviceControlUnavailableException()
        return when (val action = args.optArgString("action") ?: "elements") {
            "elements" -> parseElements(service.elements())
            "screenshot" -> capture(service)
            else -> throw DeviceToolArgsException(
                "Unknown action '$action' — expected 'elements' or 'screenshot'.",
            )
        }
    }

    private fun parseElements(json: String): JsonObject =
        runCatching { kotlinx.serialization.json.Json.parseToJsonElement(json).jsonObject }
            .getOrElse { throw DeviceToolArgsException("Could not read the screen's element list.") }

    private suspend fun capture(service: com.mewbo.aura.data.device.shizuku.IDeviceService): JsonObject {
        // Only the CAPTURE is veiled, never the element read: `uiautomator` walks the
        // accessibility tree, which our overlay is absent from anyway, so hiding the window there
        // would flicker it for no gain. The veil restores the overlay however this ends —
        // including on the throw below, which is the path a protected screen takes.
        val result = veil.hiddenDuring {
            ScreenCaptureResult.fromWire(
                service.captureScreen(ScreenCapture.DEFAULT_MAX_WIDTH, ScreenCapture.DEFAULT_QUALITY),
            )
        }
        return when (result) {
            // A failed capture must surface as TEXT: the API rejects a
            // tool_result carrying a non-text block while flagged as an error,
            // so a broken image attached to a failure would 400 the whole
            // request. The service names the cause, so relay ITS words — a
            // protected window and a display that is off are different problems
            // with different recoveries, and "could not be captured" is neither.
            is ScreenCaptureResult.Failed -> throw DeviceCaptureException(result.reason)
            is ScreenCaptureResult.Captured -> buildJsonObject {
                // The image keys are written by ScreenCapture and read back by
                // fromWire, so they are decided in ONE place; geometry is this
                // side's to add, since only the app holds the display info.
                result.toWire().forEach { (key, value) -> put(key, value) }
                control.geometry()?.let {
                    put("screen_width", it.width)
                    put("screen_height", it.height)
                }
            }
        }
    }
}
