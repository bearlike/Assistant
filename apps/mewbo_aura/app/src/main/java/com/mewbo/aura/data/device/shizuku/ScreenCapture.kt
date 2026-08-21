package com.mewbo.aura.data.device.shizuku

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import java.io.ByteArrayOutputStream
import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/**
 * The outcome of one capture attempt.
 *
 * A sealed pair rather than "the base64, or empty": an empty string forces
 * every caller to invent its own explanation, and the one it invents cannot
 * name a cause it was never told. [Failed.reason] is model-facing prose, so it
 * carries the cause AND the recovery — see [ScreenCapture.explain].
 *
 * Each member renders its OWN wire shape. A service-side `when` over the two
 * would be a second place the payload's keys are decided, free to drift from
 * the contract the server lifts the image out of.
 */
sealed interface ScreenCaptureResult {

    /** The binder payload: an image envelope, or an error envelope. */
    fun toWire(): JsonObject

    companion object {
        /**
         * Read back what [toWire] wrote, on the app side of the binder.
         *
         * The reader lives beside the writer so the key names are decided
         * ONCE. A caller parsing the envelope itself would be a second copy of
         * this shape, free to drift the day a key is renamed — and the way it
         * would fail is a screenshot silently reaching the model as text.
         *
         * Total by construction: anything unparseable, error-shaped, or
         * missing its image is a [Failed], never an exception. This crosses a
         * process boundary, so "the other side sent something unexpected" is a
         * state to report, not a crash to take a tool call down with.
         */
        fun fromWire(json: String): ScreenCaptureResult {
            val envelope = runCatching { Json.parseToJsonElement(json).jsonObject }.getOrNull()
                ?: return Failed(NO_REASON_GIVEN)
            // `as? JsonPrimitive`, never the `jsonPrimitive` accessor: the
            // accessor THROWS on a nested object or array, and this input is
            // whatever the other side actually sent.
            val error = envelope["error"] as? JsonObject
            if (error != null) {
                val message = (error["message"] as? JsonPrimitive)?.content
                return Failed(message?.takeIf { it.isNotBlank() } ?: NO_REASON_GIVEN)
            }
            val base64 = (envelope["image_base64"] as? JsonPrimitive)?.content
            return if (base64.isNullOrBlank()) Failed(NO_REASON_GIVEN) else Captured(base64)
        }

        private const val NO_REASON_GIVEN =
            "The screen could not be captured, and the device gave no reason. Ask the user to " +
                "wake and unlock the phone, then retry, or read the screen with " +
                "action='elements' instead."
    }

    /** A JPEG of the current screen, base64-encoded, no wrapping. */
    data class Captured(val base64: String) : ScreenCaptureResult {
        /** `image_base64` + `image_media_type` are the two keys
         * `_multimodal_result` on the server lifts the image out of; renaming
         * either one silently sends a screenshot to the model as text. */
        override fun toWire(): JsonObject = buildJsonObject {
            put("image_base64", base64)
            put("image_media_type", ScreenCapture.MEDIA_TYPE)
        }
    }

    /** Why no image exists, in words meaningful to whoever holds the phone. */
    data class Failed(val reason: String) : ScreenCaptureResult {
        /** The shared `{"error": {code, message}}` envelope — the shape the
         * loop's error detector recognises, so a failed capture records as a
         * failed step instead of a successful one carrying an apology. */
        override fun toWire(): JsonObject = buildJsonObject {
            put(
                "error",
                buildJsonObject {
                    put("code", ERROR_CODE)
                    put("message", reason)
                },
            )
        }

        private companion object {
            const val ERROR_CODE = "capture_failed"
        }
    }
}

/**
 * Captures the screen from inside the shell-UID service and returns it sized
 * for a vision model.
 *
 * **No MediaProjection consent dialog.** At shell UID `screencap` reads the
 * display directly, which is the same privilege tier `scrcpy` runs at — the
 * premise the whole design rests on.
 *
 * The downscale and the JPEG re-encode are not cosmetic. A raw 1440x3120 PNG
 * is ~1.9 MB on the dev device (measured); it has to cross a binder, an HTTP
 * POST and then the model's own encoder, where token cost is
 * ceil(w/28) x ceil(h/28). Starting at 1280x720 and q75 is what keeps one
 * observation near ~1-1.8k tokens instead of many times that.
 *
 * **A failed capture must SAY SO, and this is the reason the result is a
 * sealed type.** `screencap` refusing a protected window still creates the
 * output file — empty — so an existence check passes, the decode returns null,
 * and a capture that returns "" on failure hands the agent blindness with no
 * diagnostic. One `FLAG_SECURE` window (a banking app, DRM video, a password
 * field) reaches that path on an ordinary screen, so it is not a corner case.
 */
class ScreenCapture(private val exec: (String, Long) -> String) {

    /**
     * Capture the screen, or explain why not.
     *
     * `screencap`'s own combined output is kept and handed to [explain]: it is
     * the only place the real cause is ever stated (`FB is protected:
     * PERMISSION_DENIED` for a protected window), and discarding it is what
     * made every failure here indistinguishable.
     */
    fun capture(maxWidth: Int, quality: Int): ScreenCaptureResult {
        return try {
            val output = exec("screencap -p $TEMP_PATH", CAPTURE_TIMEOUT_MS)
            val file = File(TEMP_PATH)
            // Length, not existence: a refused capture leaves a ZERO-BYTE file
            // behind, so `exists()` alone reports success for the one case
            // this check exists to catch.
            if (!file.exists() || file.length() == 0L) {
                return ScreenCaptureResult.Failed(explain(output))
            }
            val bitmap = BitmapFactory.decodeFile(TEMP_PATH)
                ?: return ScreenCaptureResult.Failed(explain(output))
            val scaled = downscale(bitmap, maxWidth)
            val bytes = ByteArrayOutputStream().use { out ->
                scaled.compress(Bitmap.CompressFormat.JPEG, quality, out)
                out.toByteArray()
            }
            if (scaled !== bitmap) scaled.recycle()
            bitmap.recycle()
            // Below this a "capture" is a blank or truncated frame rather than
            // a screen — a failure, so the model retries instead of reasoning
            // about an empty image.
            if (bytes.size < MIN_VALID_BYTES) {
                ScreenCaptureResult.Failed(BLANK_FRAME)
            } else {
                ScreenCaptureResult.Captured(Base64.encodeToString(bytes, Base64.NO_WRAP))
            }
        } catch (e: Exception) {
            ScreenCaptureResult.Failed(
                "The screen could not be captured: ${e.message ?: e.javaClass.simpleName}. " +
                    "Try again, or read the screen with action='elements' instead.",
            )
        } finally {
            exec("rm -f $TEMP_PATH", CLEANUP_TIMEOUT_MS)
        }
    }

    private fun downscale(bitmap: Bitmap, maxWidth: Int): Bitmap {
        if (bitmap.width <= maxWidth) return bitmap
        val height = (bitmap.height.toLong() * maxWidth / bitmap.width).toInt().coerceAtLeast(1)
        return Bitmap.createScaledBitmap(bitmap, maxWidth, height, true)
    }

    companion object {
        /** Anthropic's documented starting point; below ~960x540 loses detail. */
        const val DEFAULT_MAX_WIDTH = 1280
        const val DEFAULT_QUALITY = 75

        /** A capture smaller than this is a failed one, not a small screen. */
        const val MIN_VALID_BYTES = 1024

        /** Decided HERE because this class chooses the encoding. A second
         * literal on the app side would be a media type that lies the day the
         * encoder changes. */
        const val MEDIA_TYPE = "image/jpeg"

        private const val TEMP_PATH = "/data/local/tmp/mewbo_capture.png"
        private const val CAPTURE_TIMEOUT_MS = 10_000L
        private const val CLEANUP_TIMEOUT_MS = 2_000L

        /**
         * Turn `screencap`'s own output into a cause and a cure.
         *
         * Pure, so the wording is testable without a device — which matters
         * because this string IS the feature: it is read by a model that will
         * act on it, and relayed to a person who never agreed to know what a
         * framebuffer is. Naming the protected-content case explicitly is the
         * point; it is the one an ordinary screen reaches, and the one whose
         * recovery ("leave that screen" / "use the element list") a caller
         * cannot guess from a bare failure.
         */
        fun explain(screencapOutput: String): String {
            val detail = screencapOutput.trim().lineSequence()
                .map { it.trim() }
                .firstOrNull { it.isNotEmpty() }
            if (detail != null && PROTECTED_MARKERS.any { detail.contains(it, ignoreCase = true) }) {
                return "The screen could not be captured — Android refused to read the display " +
                    "($detail). Something on screen is protected content: a banking or payment " +
                    "app, a password field, or DRM video. Ask the user to leave that screen, or " +
                    "read the screen with action='elements' instead."
            }
            if (detail != null) {
                return "The screen could not be captured. The device reported: $detail. " +
                    "Ask the user to wake and unlock the phone, then retry, or read the screen " +
                    "with action='elements' instead."
            }
            return "The screen could not be captured — the capture produced no image and " +
                "reported no reason. The display is usually off, locked, or showing protected " +
                "content. Ask the user to wake and unlock the phone, then retry, or read the " +
                "screen with action='elements' instead."
        }

        /** Substrings `screencap` uses when the window manager refuses it.
         * Matched case-insensitively and as substrings because the exact
         * wording differs by Android version and the failure must never fall
         * through to the generic arm on a phrasing change. */
        private val PROTECTED_MARKERS = listOf("protected", "PERMISSION_DENIED", "permission denied")

        private const val BLANK_FRAME =
            "The screen was captured but the image is blank, which usually means the display is " +
                "off or mid-transition. Ask the user to wake the phone, then retry."
    }
}
