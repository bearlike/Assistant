package com.mewbo.aura.ui.chat

import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TranscriptReducer
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The replay gate ([widgetGateDropsReplay]) — the seam that keeps a `widget_ready`
 * persisted in a session's transcript from re-rendering (and re-booting Pyodide) once the user has
 * turned the Streamlit-widgets capability OFF. `ChatViewModel.applyEvent` (the ONE ingestion seam
 * both `bind`'s history re-fold and `subscribeLive` route through) drops the event via this
 * predicate before folding; `TranscriptReducer` itself stays pure. Tested at the predicate level
 * (the ViewModel isn't plain-JVM constructible — Context-backed `SettingsStore`), then composed with
 * the REAL reducer to prove a suppressed widget never becomes a [ChatItem.Widget].
 */
class ChatWidgetGateTest {

    private val json = Json { ignoreUnknownKeys = true }

    private val widgetFrame =
        """{"type":"widget_ready","ts":"t1","payload":{"widget_id":"w1","session_id":"s1",""" +
            """"files":{"app.py":"import streamlit as st","data.json":"{}"},"requirements":[],"summary":"A chart"}}"""
    private val assistantFrame = """{"type":"assistant","ts":"t2","payload":{"text":"Here you go"}}"""

    private fun decode(raw: String): SessionEvent = SessionEvent.decode(json, raw)

    @Test
    fun `a widget_ready is dropped only when widgets are disabled`() {
        assertTrue(widgetGateDropsReplay(decode(widgetFrame), widgetsEnabled = false))
        assertFalse(widgetGateDropsReplay(decode(widgetFrame), widgetsEnabled = true))
    }

    @Test
    fun `non-widget events always pass, regardless of the flag`() {
        assertFalse(widgetGateDropsReplay(decode(assistantFrame), widgetsEnabled = false))
        assertFalse(widgetGateDropsReplay(decode(assistantFrame), widgetsEnabled = true))
    }

    @Test
    fun `with widgets OFF a persisted widget_ready never folds to a Widget item on replay`() {
        val events = listOf(decode(widgetFrame), decode(assistantFrame))
        val items = TranscriptReducer.reduce(events.filterNot { widgetGateDropsReplay(it, widgetsEnabled = false) })
        assertEquals(0, items.count { it is ChatItem.Widget })
        // The rest of the turn is untouched — only the widget is suppressed.
        assertTrue(items.any { it is ChatItem.AssistantMessage })
    }

    @Test
    fun `with widgets ON the same replay folds the Widget in as normal`() {
        val events = listOf(decode(widgetFrame), decode(assistantFrame))
        val items = TranscriptReducer.reduce(events.filterNot { widgetGateDropsReplay(it, widgetsEnabled = true) })
        assertEquals(1, items.count { it is ChatItem.Widget })
    }
}
