package com.mewbo.aura.ui.aurora

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.mewbo.aura.ui.theme.LocalAssistantExtras

// Rev E §E-2 (device-frame ground truth, overrides §3.3's 35-45% estimate): the scrimmed app reads
// as a near-uniform dark-gray field on the real device, heavier than the original eyeballed range -
// tuned within Rev E's given 55-65% band. [LocalAssistantExtras.scrimColor] still supplies the base
// color from `ui/theme` (unedited); only the alpha channel is overridden here, in this file, so the
// theme token itself doesn't need touching for a value Rev E superseded after that token was set.
private const val SCRIM_ALPHA = 0.60f

/**
 * Full-bleed scrim over the live app during overlay invocation (§3.3, §6.10, Rev E §E-2). Static
 * and state-free by design: the §7.0 timeline's 180ms fade-in belongs to whoever orchestrates the
 * full invocation sequence (the overlay host), not to this primitive - wrap it in your own
 * `AnimatedVisibility`/`animateFloatAsState` there.
 *
 * Desaturating what's beneath (§3.3: "slight desaturation of underlying app") is intentionally NOT
 * implemented: a `VoiceInteractionSession` overlay window does not own the frozen host-app surface
 * behind it - there is no compositor hook to apply a color matrix to a frame someone else already
 * composited. Documented here rather than silently dropped.
 */
@Composable
fun OverlayScrim(modifier: Modifier = Modifier) {
    val extras = LocalAssistantExtras.current
    Box(modifier = modifier.fillMaxSize().background(extras.scrimColor.copy(alpha = SCRIM_ALPHA)))
}
