package com.mewbo.aura.ui.orb

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * Debug gallery (Task G's visual verification host): all four [OrbState]s at 240dp in a 2x2 grid
 * — [FlowRow] wraps to a single column on screens narrower than ~480dp+gutters, which redroid's
 * portrait panel is — plus a 96dp row. Hosted by the debug-only `OrbShowcaseActivity`.
 */
@Composable
fun OrbShowcase(modifier: Modifier = Modifier) {
    val rms by rememberInfiniteTransition(label = "showcase-rms").animateFloat(
        initialValue = 1.5f,
        targetValue = 9f,
        animationSpec = infiniteRepeatable(tween(900, easing = LinearEasing), RepeatMode.Reverse),
        label = "showcase-rms-value",
    )

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(32.dp),
            verticalArrangement = Arrangement.spacedBy(28.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            ShowcaseLabel("240dp")
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(32.dp, Alignment.CenterHorizontally),
                verticalArrangement = Arrangement.spacedBy(32.dp),
                maxItemsInEachRow = 2,
            ) {
                OrbCell(label = "Idle", state = OrbState.Idle, size = 240.dp)
                OrbCell(label = "Listening", state = OrbState.Listening(rms), size = 240.dp)
                OrbCell(label = "Thinking", state = OrbState.Thinking, size = 240.dp)
                OrbCell(label = "Error", state = OrbState.Error, size = 240.dp)
            }

            ShowcaseLabel("96dp")
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(24.dp, Alignment.CenterHorizontally),
                verticalArrangement = Arrangement.spacedBy(24.dp),
                maxItemsInEachRow = 4,
            ) {
                OrbCell(label = "Idle", state = OrbState.Idle, size = 96.dp)
                OrbCell(label = "Listening", state = OrbState.Listening(rms), size = 96.dp)
                OrbCell(label = "Thinking", state = OrbState.Thinking, size = 96.dp)
                OrbCell(label = "Error", state = OrbState.Error, size = 96.dp)
            }
        }
    }
}

@Composable
private fun ShowcaseLabel(text: String) {
    Text(text = text, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onBackground)
}

@Composable
private fun OrbCell(label: String, state: OrbState, size: Dp) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Orb(state = state, size = size)
        Text(
            text = label,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onBackground,
        )
    }
}
