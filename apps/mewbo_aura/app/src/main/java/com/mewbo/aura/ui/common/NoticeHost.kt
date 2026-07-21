package com.mewbo.aura.ui.common

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import kotlinx.coroutines.delay

/**
 * §6.9 system toast/notice: a one-line-at-a-time state holder. [show] replaces whatever's
 * currently visible (no queueing - the spec's examples are all transient single-line notices,
 * never stacked). Provided high in the nav host via [LocalNoticeController]; call sites either read
 * the CompositionLocal directly or (once a screen exposes its own `onNotice(text)` callback) get
 * wired to [show] explicitly.
 */
class NoticeController {
    var current: String? by mutableStateOf(null)
        private set

    fun show(text: String) {
        current = text
    }

    internal fun dismiss() {
        current = null
    }

    internal companion object {
        // Spec §6.9 gives "auto-dismiss 4s" as prose, not a token - AuraMotion is the proper home
        // for a named duration constant but is out of this task's file ownership; flagged in the
        // task report as a gap for W1-A to absorb.
        const val DISMISS_DELAY_MS = 4_000L
    }
}

val LocalNoticeController = compositionLocalOf<NoticeController> {
    error("NoticeController not provided — wrap content in AuraNavHost")
}

/**
 * Bottom-anchored pill (spec §6.9): full-width minus [AuraSpacing.toastWidthInset], `listItem` text
 * at `textSecondary`, [AuraColors.surfaceNotice] fill, fade+slide in/out. Auto-dismisses itself
 * after [NoticeController.DISMISS_DELAY_MS] - callers only ever call [NoticeController.show].
 */
@Composable
fun NoticeHost(controller: NoticeController, modifier: Modifier = Modifier) {
    val current = controller.current
    // Held separately from `current` so the exit fade/slide plays against the last real message
    // instead of snapping to blank the instant dismiss() nulls it out.
    var lastText by remember { mutableStateOf("") }

    LaunchedEffect(current) {
        if (current != null) {
            lastText = current
            delay(NoticeController.DISMISS_DELAY_MS)
            controller.dismiss()
        }
    }

    AnimatedVisibility(
        visible = current != null,
        enter = fadeIn() + slideInVertically(initialOffsetY = { it }),
        exit = fadeOut() + slideOutVertically(targetOffsetY = { it }),
        modifier = modifier,
    ) {
        NoticePill(text = lastText)
    }
}

@Composable
private fun NoticePill(text: String, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            // fix-round-3 minor: without this, the pill rendered BEHIND the keyboard - the
            // bottomInset below only clears the nav bar, never the IME.
            .imePadding()
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.toastWidthInset / 2)
            // Reuses the composer's own bottom-inset token: both are bottom-anchored floating
            // elements sitting above the IME/nav region (spec doesn't give the toast its own).
            .padding(bottom = AuraSpacing.Composer.bottomInset),
        contentAlignment = Alignment.Center,
    ) {
        Surface(shape = AuraShape.radiusPill, color = AuraColors.surfaceNotice) {
            Text(
                text = text,
                style = AuraType.listItem,
                color = AuraColors.textSecondary,
                modifier = Modifier.padding(
                    horizontal = AuraSpacing.Composer.internalPadding,
                    vertical = AuraSpacing.Composer.gapTight,
                ),
            )
        }
    }
}
