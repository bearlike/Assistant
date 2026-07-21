package com.mewbo.aura.ui.chat.toolcards

import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import com.mewbo.aura.data.model.ToolCall
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * The third bespoke [ToolActionCard] renderer (`device_send_sms`). Unlike the alarm/timer cards -
 * where the "big" display line is a single short fact - the salient, irreversible fact here is WHO
 * the message went to (the task brief calls sending "irreversible and may incur carrier cost"), so
 * the recipient takes the headline line and the body is the secondary detail, truncated rather than
 * the reverse: a wrong recipient is the mistake worth surfacing at a glance, a long body is not.
 *
 * `Icons.Filled.Send` is used as-is (off-the-shelf, in the `material-icons-core` dependency already
 * on the classpath) rather than a hand-rolled local glyph like the timer card's - core already ships
 * a fitting one, so there is nothing to hand-roll.
 *
 * Unparseable args degrade to [GenericToolCard] rather than rendering a half-empty card - same
 * black-box-model-output posture as [AlarmArgs.parse].
 */
@Composable
fun SmsToolCard(call: ToolCall, modifier: Modifier = Modifier) {
    val args = remember(call.inputJson) { SmsArgs.parse(call.inputJson) }
    if (args == null) {
        GenericToolCard(call = call, modifier = modifier)
        return
    }

    ToolActionCard(icon = Icons.Filled.Send, label = "Message", modifier = modifier) {
        Text(
            text = args.to,
            style = AuraType.toolCardDisplay,
            color = AuraColors.textPrimary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        Text(
            text = args.body,
            style = AuraType.bodyMessage,
            color = AuraColors.textSecondary,
            maxLines = BodyMaxLines,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
        )
    }
}

/** Long SMS bodies (`SmsManager.divideMessage` allows several concatenated parts) truncate rather
 * than push the card's height open-ended - this is a glanceable summary, not the reference the fold
 * already provides on demand via the raw tool-call JSON. */
private const val BodyMaxLines = 4

/**
 * `device_send_sms`'s arguments, parsed off the wire's `inputJson` - the whole non-Compose half of
 * [SmsToolCard], kept pure so it is unit-testable without a device (`SmsArgsTest`).
 *
 * [parse] is TOTAL, same contract as [AlarmArgs.parse]: every malformed shape the model can emit
 * (missing `to`/`body`, either arriving as a non-string, a blank recipient or body) returns `null`
 * rather than throwing or rendering an empty card.
 */
internal data class SmsArgs(val to: String, val body: String) {
    companion object {
        fun parse(input: JsonElement?): SmsArgs? {
            val obj = input as? JsonObject ?: return null
            val to = obj.string("to")?.takeIf { it.isNotBlank() } ?: return null
            val body = obj.string("body")?.takeIf { it.isNotBlank() } ?: return null
            return SmsArgs(to = to, body = body)
        }

        /** `as? JsonPrimitive`, never the `jsonPrimitive` accessor: that one THROWS on a nested
         * object/array, which is precisely the malformed input this has to survive. */
        private fun JsonObject.string(key: String): String? = (this[key] as? JsonPrimitive)?.contentOrNull
    }
}
