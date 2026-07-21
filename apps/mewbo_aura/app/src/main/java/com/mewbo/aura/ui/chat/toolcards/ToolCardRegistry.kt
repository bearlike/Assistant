package com.mewbo.aura.ui.chat.toolcards

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Build
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.mewbo.aura.data.model.ToolCall
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraType

/**
 * `toolId` -> the composable that draws it. The presentation half of the promotion decision:
 * `data/`'s `PromotedTools` allowlist decides WHICH calls become their own
 * [com.mewbo.aura.data.model.ChatItem.ToolCard] item, and this decides what that item LOOKS like -
 * so the two evolve independently. Promoting an id here-unknown is safe by construction: it lands
 * on [GenericToolCard], never a crash and never a blank row.
 *
 * A `when` with an `else ->`, mirroring `ui/chat/ChatMessageRows.kt`'s `ActivityToolGlyphs`, rather
 * than a `Map<String, @Composable>`: the map buys nothing (no runtime registration exists, or is
 * wanted) and costs the compiler's ability to see every branch.
 */
object ToolCardRegistry {

    @Composable
    fun Render(call: ToolCall, modifier: Modifier = Modifier) {
        when (call.toolId) {
            "device_set_alarm" -> AlarmToolCard(call = call, modifier = modifier)
            "device_set_timer" -> TimerToolCard(call = call, modifier = modifier)
            "device_send_sms" -> SmsToolCard(call = call, modifier = modifier)
            else -> GenericToolCard(call = call, modifier = modifier)
        }
    }

    /**
     * Turns a wire tool id into a human header label: `device_set_alarm` -> "Device set alarm".
     * Sentence case, not Title Case - the header is a quiet label, and the reference's own action
     * cards read as one calm word ("Alarm"), never as a shouted Header.
     */
    fun humanize(toolId: String): String =
        toolId.replace('_', ' ').replace('.', ' ').trim()
            .replaceFirstChar { it.uppercase() }
            .ifEmpty { UnknownToolLabel }

    private const val UnknownToolLabel = "Tool"
}

/**
 * The fallback every promoted-but-unregistered tool lands on - the reason a new id can be added to
 * `PromotedTools` with zero ui/ work and still render something honest. Header = a generic tool
 * glyph plus the humanized id; content = the backend's own one-line [ToolCall.summary], which is
 * the only payload we can render without knowing the tool's argument shape.
 *
 * `internal` (not private): [AlarmToolCard] degrades into this when its own args don't parse, which
 * is the same "we can't do better than the summary" situation by a different route.
 */
@Composable
internal fun GenericToolCard(call: ToolCall, modifier: Modifier = Modifier) {
    ToolActionCard(
        icon = Icons.Filled.Build,
        label = ToolCardRegistry.humanize(call.toolId),
        modifier = modifier,
    ) {
        // A promoted call always ran successfully (the reducer's promotion gate requires it), so
        // there is no failure copy to write here - a summary-less call simply says so rather than
        // rendering an empty card, which would read as a broken row.
        Text(
            text = call.summary ?: "Done.",
            style = AuraType.bodyMessage,
            color = AuraColors.textPrimary,
        )
    }
}
