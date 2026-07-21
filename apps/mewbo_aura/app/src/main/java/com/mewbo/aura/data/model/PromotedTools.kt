package com.mewbo.aura.data.model

/**
 * The ONE authority for "this tool_result becomes its own top-level [ChatItem.ToolCard] instead of
 * folding into the turn's [ChatItem.ToolCallGroup]" (Gemini-style action-card promotion). Membership
 * here is a transcript-STRUCTURE decision made in data/ - which items exist - never a presentation
 * decision; which composable draws a given [ChatItem.ToolCard] is ui/'s renderer registry to own.
 * Adding an id here with no matching ui/ renderer is safe: ui/ falls back to a generic card for any
 * unrecognized [ToolCall.toolId], so this allowlist and ui/'s registry can evolve independently.
 */
object PromotedTools {
    val IDS: Set<String> = setOf("device_set_alarm", "device_set_timer", "device_send_sms")

    fun isPromoted(toolId: String): Boolean = toolId in IDS
}
