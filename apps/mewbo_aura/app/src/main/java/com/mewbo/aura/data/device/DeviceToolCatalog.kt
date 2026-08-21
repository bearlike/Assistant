package com.mewbo.aura.data.device

import android.Manifest
import com.mewbo.aura.data.device.shizuku.DeviceControlGate
import javax.inject.Inject
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * One client-executable tool ([toolId] must match `^device_[a-z0-9_]{1,48}$` - wire contract)
 * advertised to the backend via `context.device_tools`. [requiredPermission] is the
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

/** Single-method seam over [com.mewbo.aura.data.settings.SettingsStore.disabledDeviceToolIds]
 * so [DeviceToolCatalog] and [DeviceToolExecutor] stay unit-testable without an
 * Android-backed DataStore - the SAME reason [DevicePermissionChecker] exists. Bound in
 * `DeviceModule` to a `settingsStore.disabledDeviceToolIds.first()` read. */
fun interface DeviceToolGate {
    /** Tool ids the user has switched OFF in Settings; empty = every tool enabled (the default). */
    suspend fun disabledToolIds(): Set<String>
}

/**
 * Static catalog of every `device_*` tool this build ships, filtered per call to the ones whose
 * [DeviceToolDefinition.requiredPermission] is currently granted (or has none) AND which the user
 * hasn't switched off in Settings ([DeviceToolGate]). OS runtime grants remain the
 * SOLE gate for a tool's PERMISSION; the per-tool toggle is a separate user-intent layer, and the
 * two intersect here so a disabled tool is never advertised in `context.device_tools`.
 */
class DeviceToolCatalog @Inject constructor(
    private val permissionChecker: DevicePermissionChecker,
    private val gate: DeviceToolGate,
    private val controlGate: DeviceControlGate,
) {
    suspend fun availableTools(): List<DeviceToolDefinition> =
        filterAvailable(
            ALL,
            permissionChecker,
            gate.disabledToolIds(),
            deviceControlReady = controlGate.isReady(),
        )

    /**
     * Whether this session should advertise the `device_control` CAPABILITY.
     *
     * **The capability and the tools must be decided by the SAME predicate.**
     * They were not: the capability was computed from the user's toggles alone
     * while the tools additionally required Shizuku to be live, so a device with
     * the toggles on and the service down advertised the playbook skill for
     * tools it never sent. The agent then activated `device-control`, searched
     * for `device_ui`, found nothing, and had to walk the failure back to a user
     * watching their phone — a capability catalogue that lies is worse than one
     * that is merely empty.
     *
     * Asking it through `availableTools()` makes that divergence structurally
     * impossible: the answer is derived from the very list that goes on the
     * wire, so the two cannot disagree without the list itself being wrong.
     *
     * **[LIFECYCLE_TOOL_IDS] deliberately do not count.** They can be on the
     * wire with Shizuku absent, and a playbook whose every step names a tool
     * this session did not send is the exact failure above, restated. The
     * start tool's own description carries what a session in that state needs.
     */
    suspend fun advertisesDeviceControl(): Boolean =
        availableTools().any { it.toolId in CONTROL_TOOL_IDS }

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
            // --- SMS + media ---
            DeviceToolDefinition(
                toolId = "device_read_latest_sms",
                description = "Read one page of SMS messages, newest first, including the user's own sent " +
                    "replies so a conversation reads in order. `count` is the PAGE SIZE (default 10, max 50), " +
                    "NOT a per-conversation total: with no `sender_filter` a page spans the whole mailbox, so " +
                    "a long conversation can be entirely absent from it. Pass `sender_filter` — a " +
                    "case-insensitive substring of the phone number or sender address — to read ONE " +
                    "conversation, and `offset` to page further back through older messages in whatever the " +
                    "page covers. Each message carries `direction`: \"inbound\" is what the other party sent, " +
                    "\"outbound\" is what the user sent. `has_more: true` in the result means older messages " +
                    "exist that you have NOT seen — page or narrow before answering, never summarise as if " +
                    "the page were everything. Message bodies and phone numbers are personal data - only use " +
                    "when the user has clearly asked to check their texts.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject {
                            // `count`/`offset` come from the SHARED window, never hand-written
                            // here - that is what keeps the advertised bound equal to the
                            // enforced one, and keeps a future call-log read from inventing
                            // `limit`/`start` for the same idea.
                            DeviceReadWindow.SMS.schemaProperties().forEach { (key, schema) -> put(key, schema) }
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
            // --- Device control (Shizuku, shell UID) ---
            // THREE tools, not nine: the action space is multiplexed behind an
            // `action` enum. A tool schema is re-sent at full price on every
            // LLM call, so nine schemas would be a permanent tax; three is a
            // ~2/3 cut for the same capability. Each description carries the
            // CONTRACT (what it does, what the arguments mean, the
            // index-addressing rule) compressed but never empty — the model
            // reads it on every call and must stay able to use the tool if the
            // plugin's playbook has fallen out of context. The PROCEDURE lives
            // in that playbook, which is cached at ~10% of the price.
            DeviceToolDefinition(
                toolId = "device_ui",
                description = "Observe the screen. action='elements' returns a numbered list of " +
                    "interactive/text elements — cheap, and the DEFAULT observation. " +
                    "action='screenshot' returns an image, which costs roughly as much as the whole " +
                    "tool surface per call, so use it only when layout or visual state actually " +
                    "matters. Elements are addressed by their index `i` in device_action.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject {
                            put("action", enumSchema("elements", "screenshot"))
                        },
                    )
                    put("required", JsonArray(listOf(JsonPrimitive("action"))))
                },
            ),
            DeviceToolDefinition(
                toolId = "device_action",
                description = "Act on the screen, then return the settled element list so the next " +
                    "step needs no separate observation. Targets are addressed by element index " +
                    "(`index`), never coordinates. action: tap | swipe | type | key | launch | wait. " +
                    "tap/type need `index`; type also needs `text`; swipe needs `direction` " +
                    "(up|down|left|right); key needs `key` (back|home|recents|enter); launch needs " +
                    "`package_name`. A stale index returns a structured error — re-observe and retry.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject {
                            put("action", enumSchema("tap", "swipe", "type", "key", "launch", "wait"))
                            put("index", integerSchema(minimum = 0))
                            put("text", stringSchema())
                            put("direction", enumSchema("up", "down", "left", "right"))
                            put("key", enumSchema("back", "home", "recents", "enter"))
                            put("package_name", stringSchema())
                        },
                    )
                    put("required", JsonArray(listOf(JsonPrimitive("action"))))
                },
            ),
            DeviceToolDefinition(
                toolId = "device_shell",
                description = "Run a shell command on the device at shell UID (2000). The escape " +
                    "hatch for what the structured tools do not cover — prefer device_ui and " +
                    "device_action, which carry the addressing contract. Returns combined " +
                    "stdout and stderr.",
                parameters = buildJsonObject {
                    put("type", "object")
                    put(
                        "properties",
                        buildJsonObject { put("command", stringSchema()) },
                    )
                    put("required", JsonArray(listOf(JsonPrimitive("command"))))
                },
            ),
            // The lifecycle pair. NOT gated on Shizuku being live, unlike the
            // three above: naming the cause of a refusal is the whole value
            // here, and a tool withheld because the thing it would explain is
            // missing explains nothing.
            DeviceToolDefinition(
                toolId = "device_control_start",
                description = "Take control of this device's screen. Call it BEFORE device_ui, " +
                    "device_action or device_shell — they refuse until control is active. " +
                    "Returns an outcome: 'granted', 'already_active', or a refusal naming what " +
                    "the user must do (install Shizuku, start it, or authorise Aura in it). " +
                    "Safe to call again.",
                parameters = emptyObjectSchema(),
            ),
            DeviceToolDefinition(
                toolId = "device_control_stop",
                description = "Release control of this device's screen. Call it as soon as the " +
                    "task is done — an active grant holds a live connection open and shows the " +
                    "user a persistent notification. Idempotent.",
                parameters = emptyObjectSchema(),
            ),
        )

        /** The device-control tools, gated on Shizuku rather than on an Android
         * runtime permission — so they are filtered by [DeviceControlGate], not
         * by [DevicePermissionChecker]. Default-OFF, unlike the other nine:
         * driving a user's phone is opt-in. */
        val CONTROL_TOOL_IDS: Set<String> =
            setOf("device_ui", "device_action", "device_shell")

        /**
         * The grant's own start/stop pair.
         *
         * A THIRD gating class, because they answer for the other three rather
         * than being one of them: they ride the user's screen-control opt-in
         * (no opt-in, no lifecycle) but NOT Shizuku's liveness and NOT the grant
         * itself. Gating them on Shizuku would delete the only surface that can
         * say WHY Shizuku is unusable; gating them on the grant would make a
         * grant unobtainable and unreleasable.
         *
         * They carry no Settings switch of their own on purpose — a user who
         * could disable the gate while leaving the tools it guards enabled has
         * a control that reads backwards.
         */
        val LIFECYCLE_TOOL_IDS: Set<String> =
            setOf("device_control_start", "device_control_stop")

        private fun emptyObjectSchema(): JsonObject = buildJsonObject {
            put("type", "object")
            put("properties", buildJsonObject {})
        }

        private fun stringSchema(): JsonObject = buildJsonObject { put("type", "string") }

        /** `internal` rather than private so [DeviceReadWindow] emits the SAME `count`/`offset`
         * schema shape every paged read advertises, instead of a second hand-written copy. */
        internal fun integerSchema(minimum: Int, maximum: Int? = null): JsonObject = buildJsonObject {
            put("type", "integer")
            put("minimum", minimum)
            if (maximum != null) put("maximum", maximum)
        }

        private fun enumSchema(vararg values: String): JsonObject = buildJsonObject {
            put("type", "string")
            put("enum", JsonArray(values.map { JsonPrimitive(it) }))
        }

        /** Pure filter, split out of [availableTools] so it's directly testable: a tool survives iff
         * its [DeviceToolDefinition.requiredPermission] is granted (or absent) AND it is not in
         * [disabledToolIds] (the user's per-tool Settings toggle). [disabledToolIds]
         * defaults empty so pre-toggle call sites (and the permission-only tests) read unchanged.
         *
         * [LIFECYCLE_TOOL_IDS] ride the screen-control OPT-IN only: if the user has left every
         * control tool switched off they have not asked to have their phone driven, so the pair
         * that would offer it is pure context cost. Everything else about their availability is
         * deliberately absent — see [LIFECYCLE_TOOL_IDS]. */
        internal fun filterAvailable(
            definitions: List<DeviceToolDefinition>,
            checker: DevicePermissionChecker,
            disabledToolIds: Set<String> = emptySet(),
            deviceControlReady: Boolean = false,
        ): List<DeviceToolDefinition> {
            val controlOptedIn = CONTROL_TOOL_IDS.any { it !in disabledToolIds }
            return definitions.filter {
                (it.requiredPermission == null || checker.isGranted(it.requiredPermission)) &&
                    it.toolId !in disabledToolIds &&
                    (it.toolId !in CONTROL_TOOL_IDS || deviceControlReady) &&
                    (it.toolId !in LIFECYCLE_TOOL_IDS || controlOptedIn)
            }
        }
    }
}
