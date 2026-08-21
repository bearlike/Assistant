package com.mewbo.aura.ui.common

import androidx.compose.foundation.border
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Shape
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing

/**
 * Draws the D-pad focus ring while this element holds focus.
 *
 * **One implementation, applied through the shared vocabulary rather than per screen.** A ring that
 * each surface draws for itself drifts in weight and colour within a release, and on a television
 * the ring is not decoration — it is the only thing telling the viewer where the remote is. The
 * token pair lives in `ui/theme/` (`AuraColors.focusRing`, `AuraSpacing.Focus`), so this file
 * introduces no literal of its own.
 *
 * **Not gated on `TelevisionChecker`, deliberately.** Focus is not a touch state — tapping never
 * grants it — so a handheld user never sees this ring unless they have attached a keyboard or a
 * remote, in which case they want it. Gating would buy nothing and would add a second code path
 * that only ever runs on hardware nobody in the loop is holding.
 *
 * [shape] should match the silhouette of the element being ringed; the default suits the row-shaped
 * surfaces that make up most of the focusable set. A circular or pill-shaped target passes its own.
 *
 * Uses [onFocusChanged] with a plain `mutableStateOf` rather than an interaction source: the ring
 * reacts to FOCUS alone, and an interaction source would also carry press/hover, which are touch
 * states this must not respond to.
 *
 * ## ⚠️ Order matters, and getting it wrong fails SILENTLY
 *
 * **Put this BEFORE the modifier that makes the element clickable, never after.**
 *
 * ```kotlin
 * Modifier.auraFocusRing().clickable(onClick = …)   // ✅ ring draws
 * Modifier.clickable(onClick = …).auraFocusRing()   // ❌ compiles, never draws
 * ```
 *
 * [onFocusChanged] observes only the focus targets that come AFTER it in the chain, and
 * `clickable`/`combinedClickable`/`selectable`/`toggleable` each carry their own focus target. Ring
 * it from behind and the callback never fires: no crash, no warning, no lint — the element still
 * takes focus perfectly well and simply never shows it. This was measured on a television, where
 * the whole drawer held focus correctly and rendered no ring at all.
 *
 * A Material component (`IconButton`, `Switch`, `Button`) takes its `modifier` and applies its own
 * click handling AFTER it, so passing `Modifier.auraFocusRing()` as that component's `modifier` is
 * already the correct order. The trap is only on a hand-built `Box`/`Row` where you append.
 *
 * Where the click arrives inside a caller-supplied `modifier` parameter, this must lead the chain —
 * `Modifier.auraFocusRing().then(modifier)` — which also rings the element's full outer bounds
 * rather than whatever a later `padding` shrinks it to.
 */
@Composable
internal fun Modifier.auraFocusRing(
    shape: Shape = RoundedCornerShape(AuraSpacing.Focus.ringCornerRadius),
): Modifier {
    var focused by remember { mutableStateOf(false) }
    return this
        .onFocusChanged { state -> focused = state.isFocused }
        .then(
            if (focused) {
                Modifier.border(AuraSpacing.Focus.ringWidth, AuraColors.focusRing, shape)
            } else {
                Modifier
            },
        )
}
