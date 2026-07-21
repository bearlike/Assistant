package com.mewbo.aura.ui.theme

import android.provider.Settings
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
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
    // [R4 2026-07-10] The OS-level "Remove animations" accessibility setting must be honored
    // regardless of the in-app toggle (WCAG motion-sensitivity): animator scale 0 ⇒ reduced
    // motion, OR-ed (never replacing) the app's own setting. ONE seam for both hosts
    // (MainActivity + AuraSession) since both compose through this function. Read once per
    // composition — a mid-session system toggle applies from the next composition (documented
    // limitation; a ContentObserver is deliberate YAGNI until a user reports the gap).
    val context = LocalContext.current
    val systemReducedMotion = remember(context) {
        Settings.Global.getFloat(
            context.contentResolver,
            Settings.Global.ANIMATOR_DURATION_SCALE,
            1f,
        ) == 0f
    }
    val effectiveReducedMotion = reducedMotion || systemReducedMotion

    val extras = AssistantExtras(
        orbPalette = { state ->
            when (state) {
                is OrbState.Idle -> AuraOrbColors.idle
                is OrbState.Listening -> AuraOrbColors.listening
                is OrbState.Thinking -> AuraOrbColors.thinking
                is OrbState.Error -> AuraOrbColors.error
            }
        },
        reducedMotion = effectiveReducedMotion,
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
