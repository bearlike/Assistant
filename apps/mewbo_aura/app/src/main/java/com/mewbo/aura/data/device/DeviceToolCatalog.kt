package com.mewbo.aura.data.device

import android.Manifest
import javax.inject.Inject
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * One client-executable tool ([toolId] must match `^device_[a-z0-9_]{1,48}$` - wire contract,
 * Gitea #179) advertised to the backend via `context.device_tools`. [requiredPermission] is the
 * Android runtime permission (if any) that gates this tool out of [DeviceToolCatalog.availableTools]
 * when not granted - Task 2's five tools all ship with none (normal/no-permission APIs); Task 3's
 * two SMS tools are the first to actually gate on one.
 */
data class DeviceToolDefinition(
    val toolId: String,
    val description: String,
    val parameters: JsonObject,
    val requiredPermission: String? = null,
) {
    init {
        require(TOOL_ID_REGEX.matches(toolId)) { "tool_id must match ${TOOL_ID_REGEX.pattern}: $toolId" }
    }

    private companion object {
        val TOOL_ID_REGEX = Regex("^device_[a-z0-9_]{1,48}$")
    }
}

/** `{"tool_id", "description", "parameters"}` - the wire shape of one `context.device_tools`
 * entry. [DeviceToolDefinition.requiredPermission] is a client-only gating concept and never
 * crosses the wire. */
internal fun DeviceToolDefinition.toContextEntry(): JsonObject = buildJsonObject {
    put("tool_id", toolId)
    put("description", description)
    put("parameters", parameters)
}

/** Single-method seam over `Context.checkSelfPermission` so [DeviceToolCatalog] is unit-testable
 * without mocking Context statics. */
fun interface DevicePermissionChecker {
    fun isGranted(permission: String): Boolean
}

/**
 * Static catalog of every `device_*` tool this build ships, filtered per call to only the ones
 * whose [DeviceToolDefinition.requiredPermission] is currently granted (or has none) - OS runtime
 * grants are the SOLE gate, there is no separate consent toggle anywhere in the app (task brief).
 */
class DeviceToolCatalog @Inject constructor(
    private val permissionChecker: DevicePermissionChecker,
) {
    fun availableTools(): List<DeviceToolDefinition> = filterAvailable(ALL, permissionChecker)

    companion object {
        val ALL: List<DeviceToolDefinition> = listOf(
            DeviceToolDefinition(
                toolId = "device_get_time",
                description = "Get the device's current local date/time and IANA timezone.",
                parameters = emptyObjectSchema(),
            ),
            DeviceToolDefinition(
                toolId = "device_get_battery",
                description = "Get the device's current battery level and charging state.",
                parameters = emptyObjectSchema(),
            ),
            DeviceToolDefinition(
                toolId = "device_set_alarm",
                description = "Hand an alarm request off to the device's clock app for a given hour/minute.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject {
                            put("hour", integerSchema(minimum = 0, maximum = 23))
                            put("minute", integerSchema(minimum = 0, maximum = 59))
                            put("message", stringSchema())
                        },
                    )
                    put("required", JsonArray(listOf(JsonPrimitive("hour"), JsonPrimitive("minute"))))
                },
            ),
            DeviceToolDefinition(
                toolId = "device_set_timer",
                description = "Hand a countdown timer request off to the device's clock app.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject {
                            put("seconds", integerSchema(minimum = 1))
                            put("label", stringSchema())
                        },
                    )
                    put("required", JsonArray(listOf(JsonPrimitive("seconds"))))
                },
            ),
            DeviceToolDefinition(
                toolId = "device_wake",
                description = "Ring and vibrate the device briefly (alarm-stream volume) to get the user's attention.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put("properties", buildJsonObject { put("reason", stringSchema()) })
                },
            ),
            // --- Phase 4 (Gitea #179) ---
            DeviceToolDefinition(
                toolId = "device_read_latest_sms",
                description = "Read the most recent SMS message(s) from the inbox, optionally filtered by sender. " +
                    "Message bodies and phone numbers are personal data - only use when the user has clearly asked to check their texts.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject {
                            put("count", integerSchema(minimum = 1, maximum = 5))
                            put("sender_filter", stringSchema())
                        },
                    )
                },
                requiredPermission = Manifest.permission.READ_SMS,
            ),
            DeviceToolDefinition(
                toolId = "device_send_sms",
                description = "Send an SMS text message. This is irreversible and may incur carrier cost - only use when " +
                    "the user has explicitly asked to send a text, with a recipient and message they've confirmed.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject {
                            put("to", stringSchema())
                            put("body", stringSchema())
                        },
                    )
                    put("required", JsonArray(listOf(JsonPrimitive("to"), JsonPrimitive("body"))))
                },
                requiredPermission = Manifest.permission.SEND_SMS,
            ),
            DeviceToolDefinition(
                toolId = "device_get_next_alarm",
                description = "Get the single next scheduled alarm system-wide. Android exposes no API to list every " +
                    "alarm from every app - only the one soonest to fire.",
                parameters = emptyObjectSchema(),
            ),
            DeviceToolDefinition(
                toolId = "device_dismiss_alarm",
                description = "Hand an alarm-dismiss request off to the device's clock app: by a specific time " +
                    "(hour+minute), by label, the next alarm, or all alarms.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject {
                            put("search_mode", enumSchema("time", "label", "next", "all"))
                            put("hour", integerSchema(minimum = 0, maximum = 23))
                            put("minute", integerSchema(minimum = 0, maximum = 59))
                            put("label", stringSchema())
                        },
                    )
                    put("required", JsonArray(listOf(JsonPrimitive("search_mode"))))
                },
            ),
        )

        private fun emptyObjectSchema(): JsonObject = buildJsonObject {
            put("type", "object")
            put("properties", buildJsonObject {})
        }

        private fun stringSchema(): JsonObject = buildJsonObject { put("type", "string") }

        private fun integerSchema(minimum: Int, maximum: Int? = null): JsonObject = buildJsonObject {
            put("type", "integer")
            put("minimum", minimum)
            if (maximum != null) put("maximum", maximum)
        }

        private fun enumSchema(vararg values: String): JsonObject = buildJsonObject {
            put("type", "string")
            put("enum", JsonArray(values.map { JsonPrimitive(it) }))
        }

        /** Pure filter, split out of [availableTools] so it's directly testable against a synthetic
         * [DeviceToolDefinition] carrying a [DeviceToolDefinition.requiredPermission] - none of
         * [ALL]'s current defs gate on a real permission yet (Task 2 ships none that need one). */
        internal fun filterAvailable(
            definitions: List<DeviceToolDefinition>,
            checker: DevicePermissionChecker,
        ): List<DeviceToolDefinition> =
            definitions.filter { it.requiredPermission == null || checker.isGranted(it.requiredPermission) }
    }
}
