package com.mewbo.aura.ui.aurora

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.unit.dp
import com.mewbo.aura.ui.orb.AuraSpark
import com.mewbo.aura.ui.orb.OrbShowcase
import com.mewbo.aura.ui.orb.SparkState
import com.mewbo.aura.ui.theme.AuraMotion
import kotlinx.coroutines.delay

// "Bottom glow" retired Gitea #181 (AuroraGlowBottom deleted - the wash's own new Resting state,
// exercised on the Wash page, replaced it as the landing/idle liveness layer).
private val PAGE_TITLES = listOf("Wash", "Edge glow", "Scrim", "Spark", "Orb")

/**
 * Debug gallery (Task W1-B's visual verification host): pages through every aurora primitive plus
 * the existing [OrbShowcase] orb states, one full-bleed page at a time (click-to-switch rather than
 * a swipeable pager, so scripted screenshot capture can address each page by a fixed index).
 * Hosted by the debug-only `LivenessShowcaseActivity`.
 */
@Composable
fun LivenessShowcase(modifier: Modifier = Modifier) {
    var pageIndex by remember { mutableIntStateOf(0) }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(Modifier.fillMaxSize()) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                TextButton(onClick = { pageIndex = (pageIndex - 1 + PAGE_TITLES.size) % PAGE_TITLES.size }) {
                    Text("< Prev")
                }
                Text(
                    text = "${pageIndex + 1}/${PAGE_TITLES.size}  ${PAGE_TITLES[pageIndex]}",
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onBackground,
                )
                TextButton(onClick = { pageIndex = (pageIndex + 1) % PAGE_TITLES.size }) {
                    Text("Next >")
                }
            }
            Box(Modifier.fillMaxWidth().weight(1f)) {
                when (pageIndex) {
                    0 -> WashPage()
                    1 -> EdgeGlowPage()
                    2 -> ScrimPage()
                    3 -> SparkPage()
                    else -> OrbShowcase()
                }
            }
        }
    }
}

@Composable
private fun WashPage() {
    // Defaults to Resting (Gitea #181: the new landing/idle state - the page most worth checking
    // first now that AuroraGlowBottom is retired).
    var state by remember { mutableStateOf<AuroraState>(AuroraState.Resting) }
    Box(Modifier.fillMaxSize()) {
        AuroraWashTop(state = state)
        Text(
            text = "Sample content over the wash",
            modifier = Modifier.align(Alignment.Center),
            color = MaterialTheme.colorScheme.onBackground,
        )
        Row(
            modifier = Modifier.align(Alignment.TopCenter).padding(top = 16.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            TextButton(onClick = { state = AuroraState.Hidden }) { Text("Hidden") }
            TextButton(onClick = { state = AuroraState.Resting }) { Text("Resting") }
            TextButton(onClick = { state = AuroraState.Thinking }) { Text("Thinking") }
            TextButton(onClick = { state = AuroraState.Streaming }) { Text("Streaming") }
        }
    }
}

@Composable
private fun EdgeGlowPage() {
    var state by remember { mutableStateOf<EdgeGlowState>(EdgeGlowState.Listening(6f)) }
    var replayTick by remember { mutableIntStateOf(0) }
    var replaying by remember { mutableStateOf(false) }
    val replayProgress = remember { mutableFloatStateOf(0f) }

    LaunchedEffect(replayTick) {
        if (replayTick == 0) return@LaunchedEffect
        replaying = true
        val steps = 30
        repeat(steps + 1) { i ->
            replayProgress.floatValue = i / steps.toFloat()
            state = EdgeGlowState.Igniting(replayProgress.floatValue)
            delay(AuraMotion.edgeSweepMs.toLong() / steps)
        }
        state = EdgeGlowState.Listening(6f)
        delay(1800)
        state = EdgeGlowState.Thinking
        delay(1800)
        replaying = false
    }

    Box(Modifier.fillMaxSize()) {
        AuroraEdgeGlow(state = state)
        Column(
            modifier = Modifier.align(Alignment.TopCenter).padding(top = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(state.debugLabel(), color = MaterialTheme.colorScheme.onBackground)
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                TextButton(onClick = { state = EdgeGlowState.Hidden }) { Text("Hidden") }
                TextButton(onClick = { state = EdgeGlowState.Igniting(1f) }) { Text("Ignited") }
                TextButton(onClick = { state = EdgeGlowState.Listening(1f) }) { Text("Listen (quiet)") }
                TextButton(onClick = { state = EdgeGlowState.Listening(9f) }) { Text("Listen (loud)") }
                TextButton(onClick = { state = EdgeGlowState.Thinking }) { Text("Think") }
            }
            TextButton(enabled = !replaying, onClick = { replayTick++ }) {
                Text(if (replaying) "Replaying…" else "Replay: Ignite -> Listen -> Think")
            }
        }
    }
}

private fun EdgeGlowState.debugLabel(): String = when (this) {
    EdgeGlowState.Hidden -> "Hidden"
    is EdgeGlowState.Igniting -> "Igniting (${(progress * 100).toInt()}%)"
    is EdgeGlowState.Listening -> "Listening (rms=$rmsDb dB)"
    EdgeGlowState.Thinking -> "Thinking"
}

@Composable
private fun ScrimPage() {
    var visible by remember { mutableStateOf(true) }
    Box(Modifier.fillMaxSize()) {
        // Fake "frozen host app" placeholder so the scrim's darkening reads against something.
        Box(
            Modifier
                .fillMaxSize()
                .background(
                    Brush.verticalGradient(
                        listOf(
                            MaterialTheme.colorScheme.primaryContainer,
                            MaterialTheme.colorScheme.tertiaryContainer,
                        ),
                    ),
                ),
        )
        AnimatedVisibility(
            visible = visible,
            enter = fadeIn(tween(AuraMotion.scrimFadeMs)),
            exit = fadeOut(tween(AuraMotion.scrimFadeMs)),
        ) {
            OverlayScrim()
        }
        TextButton(
            onClick = { visible = !visible },
            modifier = Modifier.align(Alignment.TopCenter).padding(top = 16.dp),
        ) {
            Text(if (visible) "Hide scrim" else "Show scrim")
        }
    }
}

@Composable
private fun SparkPage() {
    var state by remember { mutableStateOf<SparkState>(SparkState.Shimmer) }
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        AuraSpark(state = state, size = 160.dp)
        Spacer(Modifier.height(24.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            TextButton(onClick = { state = SparkState.Shimmer }) { Text("Shimmer") }
            TextButton(onClick = { state = SparkState.Thinking }) { Text("Thinking") }
        }
    }
}
