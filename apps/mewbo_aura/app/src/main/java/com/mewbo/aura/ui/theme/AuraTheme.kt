package com.mewbo.aura.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import com.mewbo.aura.ui.orb.OrbState

/**
 * The single theme entry composable (design-v2, spec §2: dark-only). Wires the single dark M3
 * `ColorScheme` (see [AuraLegacyColorScheme]) + [AuraShape.Shapes] + [AuraType.Typography] through
 * [MaterialTheme] (`MaterialExpressiveTheme` is `internal` in the resolved material3 1.4.0 AAR —
 * see [AuraMotion]), and provides [AssistantExtras] for the orb + scrim tokens.
 */
@Composable
fun AuraTheme(
    reducedMotion: Boolean = false,
    content: @Composable () -> Unit,
) {
    val extras = AssistantExtras(
        orbPalette = { state ->
            when (state) {
                is OrbState.Idle -> AuraOrbColors.idle
                is OrbState.Listening -> AuraOrbColors.listening
                is OrbState.Thinking -> AuraOrbColors.thinking
                is OrbState.Error -> AuraOrbColors.error
            }
        },
        scrimColor = AuraColors.scrimOverlay,
        reducedMotion = reducedMotion,
    )

    CompositionLocalProvider(LocalAssistantExtras provides extras) {
        MaterialTheme(
            colorScheme = AuraLegacyColorScheme,
            shapes = AuraShape.Shapes,
            typography = AuraType.Typography,
            content = content,
        )
    }
}
