package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.ShizukuDeviceControl
import javax.inject.Inject
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * `device_shell` — a shell command at shell UID (2000).
 *
 * The escape hatch for what the structured tools do not cover. It ships
 * because under a Shizuku mandate the shell IS the substrate every structured
 * tool sits on, so withholding it buys no safety — it only forces hand-wrapping
 * `am`/`pm`/`settings`/`dumpsys` one at a time. Anthropic ships `bash` in every
 * tool group for the same reason.
 *
 * It carries its own Settings toggle, separate from the GUI tools, because the
 * blast radius genuinely differs: this can uninstall packages and read other
 * apps' logs, and prompt injection reaches it through any screen it captures.
 * The deny list refuses the handful of commands whose damage is not recoverable
 * by the user noticing and stopping the run.
 */
class DeviceShellHandler @Inject constructor(
    private val control: ShizukuDeviceControl,
) : DeviceToolHandler {
    override val toolId: String = "device_shell"

    override suspend fun execute(args: JsonObject): JsonObject {
        val service = control.service() ?: throw DeviceControlUnavailableException()
        val command = args.requireArgString("command")
        DENIED.firstOrNull { it.containsMatchIn(command) }?.let {
            throw DeviceToolArgsException(
                "This command is not permitted from device_shell — it is irreversible or would " +
                    "disable the assistant's own access.",
            )
        }
        val output = service.shell(command, TIMEOUT_MS)
        return buildJsonObject {
            put("command", command)
            put("output", output)
        }
    }

    private companion object {
        /** Under the 30s dispatch budget with room for the round trip. A
         * command needing longer is unrepresentable here by design. */
        const val TIMEOUT_MS = 20_000

        /**
         * Refused outright. Each entry is something whose effect the user
         * cannot undo by watching and stopping the run: wiping data, removing
         * the assistant or the very service that grants this access, or
         * rebooting out from under a live session.
         */
        val DENIED = listOf(
            Regex("""\bpm\s+(uninstall|clear|disable)"""),
            Regex("""\b(reboot|shutdown)\b"""),
            Regex("""\brm\s+-rf?\s+/(?!data/local/tmp)"""),
            Regex("""\bsvc\s+(power|wifi|data)\b"""),
            Regex("""\bsettings\s+put\s+global\s+(http_proxy|adb_enabled)"""),
            Regex("""\bam\s+force-stop\s+(moe\.shizuku|com\.mewbo\.aura)"""),
        )
    }
}
