package com.mewbo.aura.data.device.shizuku

import android.os.Build
import androidx.annotation.Keep
import java.io.BufferedReader
import java.util.concurrent.TimeUnit

/**
 * The device-control implementation, running inside a Shizuku UserService at
 * shell UID (2000).
 *
 * **This class does not run in the app's process.** Shizuku's server starts it
 * as a separate `app_process` at uid 2000 and hands the app a binder to it, so
 * nothing here may touch app state, Hilt, or any app singleton — only what it
 * is passed over the binder.
 *
 * The service is BOUND ONCE and kept, which is the whole point of the design: a
 * per-action `newProcess` would spawn an `app_process` JVM per tap, and the
 * published cost of that is hundreds of milliseconds to ~1s each. At 20 actions
 * a task that alone can exhaust a 30s dispatch budget having accomplished
 * nothing. Here an action is one message on an already-open channel.
 */
@Keep
class DeviceUserService : IDeviceService.Stub {

    @Keep
    constructor()

    /** Shizuku API v13+ also offers a Context constructor; kept so the server
     * can use either without the class disappearing under R8. */
    @Keep
    constructor(context: android.content.Context) : this() {
        // Nothing to hold — every call is self-contained.
    }

    /**
     * Reserved by the Shizuku server, which calls it on unbind. Without the
     * explicit exit the service process outlives the app that bound it.
     */
    override fun destroy() {
        System.exit(0)
    }

    override fun displayInfo(): String {
        val size = exec("wm size", DEFAULT_TIMEOUT_MS)
        val density = exec("wm density", DEFAULT_TIMEOUT_MS)
        val geometry = DisplayGeometry.parse(size, density) ?: return ""
        return "${geometry.width}x${geometry.height}:${geometry.density}"
    }

    override fun elements(): String = UiSnapshot.readPrunedJson(::exec)

    override fun captureScreen(maxWidth: Int, quality: Int): String =
        ScreenCapture(::exec).capture(maxWidth, quality).toWire().toString()

    override fun tap(x: Int, y: Int): Boolean =
        execOk("input tap $x $y")

    override fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int): Boolean =
        execOk("input swipe $x1 $y1 $x2 $y2 $durationMs")

    override fun typeText(text: String): Boolean =
        execOk("input text ${shellQuote(text)}")

    override fun pressKey(key: String): Boolean {
        val code = KEY_CODES[key] ?: return false
        return execOk("input keyevent $code")
    }

    override fun launch(packageName: String): Boolean {
        val output = exec(
            "monkey -p ${shellQuote(packageName)} -c android.intent.category.LAUNCHER 1",
            DEFAULT_TIMEOUT_MS,
        )
        return !output.contains("No activities found") && !output.contains("Error")
    }

    override fun shell(command: String, timeoutMs: Int): String = exec(command, timeoutMs.toLong())

    /**
     * Run a command in this (shell-UID) process and return stdout+stderr.
     *
     * The timeout is enforced here rather than left to the caller: an
     * unbounded command would hold the binder thread past the server's own
     * 30s dispatch budget, turning one slow command into a dead tool call.
     */
    private fun exec(command: String, timeoutMs: Long): String {
        val process = ProcessBuilder("sh", "-c", command)
            .redirectErrorStream(true)
            .start()
        return try {
            val output = process.inputStream.bufferedReader().use(BufferedReader::readText)
            if (!process.waitFor(timeoutMs, TimeUnit.MILLISECONDS)) {
                process.destroyForcibly()
                return output + "\n[command exceeded ${timeoutMs}ms and was terminated]"
            }
            output
        } catch (e: Exception) {
            "[command failed: ${e.message}]"
        } finally {
            process.destroyForcibly()
        }
    }

    private fun execOk(command: String): Boolean {
        val output = exec(command, DEFAULT_TIMEOUT_MS)
        return !output.contains("Error", ignoreCase = true) &&
            !output.contains("Exception", ignoreCase = true)
    }

    private companion object {
        const val DEFAULT_TIMEOUT_MS = 10_000L

        val KEY_CODES = mapOf(
            "back" to 4,
            "home" to 3,
            "recents" to 187,
            "enter" to 66,
        )

        /** Single-quote for `sh -c`, escaping embedded quotes. Model output is
         * a black box, so text destined for a shell word is always quoted. */
        fun shellQuote(value: String): String = "'" + value.replace("'", "'\\''") + "'"

        @Suppress("unused")
        val SDK: Int = Build.VERSION.SDK_INT
    }
}
