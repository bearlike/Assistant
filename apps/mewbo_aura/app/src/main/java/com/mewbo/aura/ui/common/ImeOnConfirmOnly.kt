package com.mewbo.aura.ui.common

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.platform.LocalSoftwareKeyboardController

/**
 * Opens the soft keyboard only when the user ASKS for it, on the shapes where focus is not that ask.
 *
 * Why focus and intent-to-type come apart at all is
 * [com.mewbo.aura.data.device.DeviceShape.opensKeyboardOnFocus]'s to explain, and this reads that
 * member rather than asking what kind of device it is — the modifier is named for the behaviour
 * because the behaviour, not the hardware, is what a caller is choosing.
 *
 * The ask is the remote's OK button, which the input framework delivers as [Key.DirectionCenter] and
 * some remotes as [Key.Enter] / [Key.NumPadEnter]. Once the keyboard is up, BACK closes it and
 * leaves focus ON the field — that is the platform's own behaviour, not something coded here, and it
 * is what lets the next arrow press navigate away through [dpadFocusEscape] instead of dismissing an
 * IME again.
 *
 * **Where the keyboard already opens on focus this returns the receiver completely unchanged.** Not
 * "an equivalent chain" — the same object, no focus observer and no key handler added, so the touch
 * path cannot regress at all. That is also why the branch is safe to write as an early return: a
 * device does not change shape mid-process, so the composition never takes the other arm later.
 *
 * Ordering: apply to the modifier handed to the text field itself, so the preview key handler is
 * offered the confirm key before the field's own editing logic claims it. Order against
 * [dpadFocusEscape] does not matter — the two claim disjoint keys (arrows there, confirm here) — but
 * both must sit on the field's own modifier for either to see the event.
 */
@Composable
internal fun Modifier.imeOnConfirmOnly(): Modifier {
    if (LocalDeviceShape.current.opensKeyboardOnFocus) return this
    val keyboard = LocalSoftwareKeyboardController.current
    return onFocusChanged { state -> if (state.isFocused) keyboard?.hide() }
        .onPreviewKeyEvent { event ->
            if (event.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
            when (event.key) {
                Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> {
                    keyboard?.show()
                    // Consumed, so a confirm that opens the keyboard cannot ALSO insert a newline
                    // into the draft it just opened over.
                    true
                }
                else -> false
            }
        }
}
