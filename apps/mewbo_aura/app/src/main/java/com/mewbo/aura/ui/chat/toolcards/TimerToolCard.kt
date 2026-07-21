package com.mewbo.aura.ui.chat.toolcards

import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.ToolCall
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.ui.theme.VectorGlyphFill
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull

/**
 * The second bespoke [ToolActionCard] renderer (`device_set_timer`), mirroring [AlarmToolCard]'s
 * shape exactly: header = glyph + quiet label, content = the duration as one big display line, plus
 * the timer's own label underneath when the model set one.
 *
 * Same handoff-not-confirmation honesty as the alarm card: the copy states the countdown that was
 * asked for, never a claim that it is now running.
 *
 * Unparseable args degrade to [GenericToolCard] rather than rendering a half-empty timer - same
 * black-box-model-output posture as [AlarmArgs.parse].
 */
@Composable
fun TimerToolCard(call: ToolCall, modifier: Modifier = Modifier) {
    val args = remember(call.inputJson) { TimerArgs.parse(call.inputJson) }
    if (args == null) {
        GenericToolCard(call = call, modifier = modifier)
        return
    }

    ToolActionCard(icon = TimerGlyph, label = "Timer", modifier = modifier) {
        Text(
            text = args.displayDuration,
            style = AuraType.toolCardDisplay,
            color = AuraColors.textPrimary,
        )
        if (args.label != null) {
            Text(
                text = args.label,
                style = AuraType.bodyMessage,
                color = AuraColors.textSecondary,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
    }
}

/**
 * `device_set_timer`'s arguments, parsed off the wire's `inputJson` and formatted for display - the
 * whole non-Compose half of [TimerToolCard], kept pure so it is unit-testable without a device
 * (`TimerArgsTest`).
 *
 * [parse] is TOTAL, same contract as [AlarmArgs.parse]: every malformed shape the model can emit
 * returns `null` rather than throwing.
 */
internal data class TimerArgs(val seconds: Int, val label: String?) {

    /** A countdown readout, not a wall-clock time - `H:MM:SS` once an hour is crossed, `M:SS`
     * otherwise, matching how the reference clock app's own timer face reads. Built by hand
     * (`padStart`), same reasoning as [AlarmArgs.displayTime]: a duration is a plain count, and a
     * locale-aware formatter risks non-Latin digits for no benefit here. */
    val displayDuration: String
        get() {
            val hours = seconds / SECONDS_PER_HOUR
            val minutes = (seconds % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE
            val secs = seconds % SECONDS_PER_MINUTE
            val secsPart = secs.toString().padStart(2, '0')
            return if (hours > 0) {
                "$hours:${minutes.toString().padStart(2, '0')}:$secsPart"
            } else {
                "$minutes:$secsPart"
            }
        }

    companion object {
        fun parse(input: JsonElement?): TimerArgs? {
            val obj = input as? JsonObject ?: return null
            val seconds = (obj["seconds"] as? JsonPrimitive)?.intOrNull ?: return null
            if (seconds < 1) return null
            return TimerArgs(
                seconds = seconds,
                // Blank is not a label - it would render an empty second line under the duration.
                label = (obj["label"] as? JsonPrimitive)?.contentOrNull?.takeIf { it.isNotBlank() },
            )
        }

        private const val SECONDS_PER_HOUR = 3600
        private const val SECONDS_PER_MINUTE = 60
    }
}

/**
 * Local stopwatch glyph for the timer card's header. `material-icons-core` (this app's curated
 * icon dependency, `ui/chat/ChatIcons.kt`'s KDoc) carries no timer/stopwatch glyph at all - same gap
 * that made `ChatIcons.Clock` a hand-roll - and `material-icons-extended` isn't in the catalog. Kept
 * LOCAL to this file rather than added to `ChatIcons` (owned by another lane): the reference is
 * Material's own "timer" glyph (button + hand + ring, ring punched by opposite winding direction
 * exactly like `ChatIcons.Clock`'s face) - deliberately not the alarm card's clock-face glyph, so
 * the two promoted cards read as visually distinct tools at a glance.
 */
private val TimerGlyph: ImageVector by lazy {
    ImageVector.Builder(name = "Timer", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
        .path(fill = SolidColor(VectorGlyphFill)) {
            // Top button.
            moveTo(15f, 1f)
            horizontalLineTo(9f)
            verticalLineToRelative(2f)
            horizontalLineToRelative(6f)
            close()
            // Hand.
            moveTo(11f, 14f)
            horizontalLineToRelative(2f)
            lineTo(13f, 8f)
            horizontalLineToRelative(-2f)
            close()
            // Outer body, incl. the side-knob notch.
            moveTo(19.03f, 7.39f)
            lineToRelative(1.42f, -1.42f)
            curveToRelative(-0.43f, -0.51f, -0.9f, -0.99f, -1.41f, -1.41f)
            lineTo(17.62f, 5.98f)
            curveTo(16.07f, 4.74f, 14.12f, 4f, 12f, 4f)
            curveToRelative(-4.97f, 0f, -9f, 4.03f, -9f, 9f)
            reflectiveCurveToRelative(4.02f, 9f, 9f, 9f)
            reflectiveCurveToRelative(9f, -4.03f, 9f, -9f)
            curveTo(21f, 10.88f, 20.26f, 8.93f, 19.03f, 7.39f)
            close()
            // Inner face - opposite winding from the outer body punches the ring hole.
            moveTo(12f, 20f)
            curveToRelative(-3.87f, 0f, -7f, -3.13f, -7f, -7f)
            reflectiveCurveToRelative(3.13f, -7f, 7f, -7f)
            reflectiveCurveToRelative(7f, 3.13f, 7f, 7f)
            reflectiveCurveToRelative(-3.13f, 7f, -7f, 7f)
            close()
        }
        .build()
}
