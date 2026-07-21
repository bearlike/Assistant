package com.mewbo.aura.ui.chat.toolcards

import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import com.mewbo.aura.data.model.ToolCall
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull

/**
 * The first bespoke [ToolActionCard] renderer (`device_set_alarm`), and the proof that the base
 * component's header + content-slot anatomy carries a real tool: header = clock + "Alarm", content
 * = the time as one big display line, plus the alarm's own label underneath when the model set one.
 *
 * The copy deliberately does NOT claim a *confirmed* alarm ("Alarm set for 8:00 AM"). The tool is an
 * explicit handoff to the clock app - the user still lands in it - so the card states the alarm it
 * asked for and stops there; promising a set alarm we can't observe would be the card lying.
 *
 * Unparseable args degrade to [GenericToolCard] rather than rendering a half-empty alarm. Tool
 * arguments are model output, i.e. a black box that may not match the schema - a card must never
 * crash or blank on them.
 */
@Composable
fun AlarmToolCard(call: ToolCall, modifier: Modifier = Modifier) {
    val args = remember(call.inputJson) { AlarmArgs.parse(call.inputJson) }
    if (args == null) {
        GenericToolCard(call = call, modifier = modifier)
        return
    }

    ToolActionCard(icon = ChatIcons.Clock, label = "Alarm", modifier = modifier) {
        Text(
            text = args.displayTime,
            style = AuraType.toolCardDisplay,
            color = AuraColors.textPrimary,
        )
        if (args.message != null) {
            Text(
                text = args.message,
                style = AuraType.bodyMessage,
                color = AuraColors.textSecondary,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
    }
}

/**
 * `device_set_alarm`'s arguments, parsed off the wire's `inputJson` and formatted for display - the
 * whole non-Compose half of [AlarmToolCard], kept pure so it is unit-testable without a device
 * (`AlarmArgsTest`).
 *
 * [parse] is TOTAL: every malformed shape the model can emit (missing keys, out-of-range clock
 * values, a nested object where a number belongs, a bare JSON string) returns `null` rather than
 * throwing, which is what lets the card degrade instead of crashing the transcript. Note this is
 * deliberately forgiving about a numeric field arriving as a JSON *string* (`"hour": "8"`) - that's
 * a shape difference, and normalizing shape is exactly what this layer is for.
 */
internal data class AlarmArgs(val hour: Int, val minute: Int, val message: String?) {

    /** The reference app's own display form, verbatim: `8:00 AM` - 12-hour, no leading zero on the
     * hour, zero-padded minutes. Built by hand rather than via `String.format`/`DateTimeFormatter`
     * so it can't pick up a device locale's 24-hour clock or Eastern-Arabic digits: the alarm the
     * model set is a wall-clock fact, and this line must read identically wherever it renders. */
    val displayTime: String
        get() {
            val hour12 = if (hour % HOURS_PER_HALF_DAY == 0) HOURS_PER_HALF_DAY else hour % HOURS_PER_HALF_DAY
            val meridiem = if (hour < HOURS_PER_HALF_DAY) "AM" else "PM"
            return "$hour12:${minute.toString().padStart(2, '0')} $meridiem"
        }

    companion object {
        fun parse(input: JsonElement?): AlarmArgs? {
            val obj = input as? JsonObject ?: return null
            val hour = obj.int("hour") ?: return null
            val minute = obj.int("minute") ?: return null
            if (hour !in 0..MAX_HOUR || minute !in 0..MAX_MINUTE) return null
            return AlarmArgs(
                hour = hour,
                minute = minute,
                // Blank is not a label - it would render an empty second line under the time.
                message = (obj["message"] as? JsonPrimitive)?.contentOrNull?.takeIf { it.isNotBlank() },
            )
        }

        /** `as? JsonPrimitive`, never the `jsonPrimitive` accessor: that one THROWS on a nested
         * object/array, which is precisely the malformed input this has to survive. */
        private fun JsonObject.int(key: String): Int? = (this[key] as? JsonPrimitive)?.intOrNull

        private const val HOURS_PER_HALF_DAY = 12
        private const val MAX_HOUR = 23
        private const val MAX_MINUTE = 59
    }
}
