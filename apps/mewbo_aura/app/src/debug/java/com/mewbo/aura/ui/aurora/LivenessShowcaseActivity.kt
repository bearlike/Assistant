package com.mewbo.aura.ui.aurora

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.mewbo.aura.ui.theme.AuraTheme

/**
 * Debug-only launcher for the design-v2 visual verification loop (Task W1-B) - deliberately
 * independent of `MainActivity`, same rationale as the orb v2 showcase it supersedes: never
 * collides with the concurrent navigation rewrite. `AuraTheme` is dark-only (spec §2), so no mode
 * argument is needed. Disables the system's automatic 3-button-nav contrast scrim: [AuroraEdgeGlow]
 * is full-bleed edge-to-edge content, and that scrim otherwise paints an opaque grey band directly
 * over its bottom bloom (where the D-3 clay ignition stop lives), making it un-screenshottable
 * on any device without gesture navigation - this is a showcase-host concern, not a shader bug.
 * Launch directly: `adb -s <serial> shell am start -n com.mewbo.aura/.ui.aurora.LivenessShowcaseActivity`.
 */
class LivenessShowcaseActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        window.isNavigationBarContrastEnforced = false
        setContent {
            AuraTheme {
                LivenessShowcase()
            }
        }
    }
}
