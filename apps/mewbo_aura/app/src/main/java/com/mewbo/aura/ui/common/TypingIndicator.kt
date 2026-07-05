package com.mewbo.aura.ui.common

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.unit.dp

/**
 * Three-dot pulse shown while awaiting the first response token (ui/chat: Sending / streaming
 * with no delta yet). Each dot's opacity breathes on its own phase offset so the pulse reads as a
 * wave rather than three dots blinking in lockstep.
 */
@Composable
fun TypingIndicator(modifier: Modifier = Modifier, label: String? = null) {
    Row(modifier = modifier, horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
        repeat(DOT_COUNT) { index ->
            val infinite = rememberInfiniteTransition(label = "typing-dot-$index")
            val alpha by infinite.animateFloat(
                initialValue = 0.25f,
                targetValue = 1f,
                animationSpec = infiniteRepeatable(
                    animation = tween(PULSE_DURATION_MS, easing = LinearEasing),
                    repeatMode = RepeatMode.Reverse,
                    initialStartOffset = androidx.compose.animation.core.StartOffset(index * PULSE_DURATION_MS / DOT_COUNT),
                ),
                label = "typing-dot-alpha-$index",
            )
            Box(
                modifier = Modifier
                    .size(6.dp)
                    .alpha(alpha)
                    .background(color = LocalContentColor.current, shape = CircleShape),
            )
        }
        if (label != null) {
            Text(text = label, style = MaterialTheme.typography.labelSmall)
        }
    }
}

private const val DOT_COUNT = 3
private const val PULSE_DURATION_MS = 600
