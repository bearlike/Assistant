package com.mewbo.aura.ui.common

import androidx.compose.foundation.focusGroup
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusProperties

/**
 * Keeps D-pad focus inside the app's own content.
 *
 * **Why containment and not recovery — this was measured, not chosen.** Focus escaping the rendered
 * tree is unrecoverable on a television: once nothing holds focus, Compose dispatches no key event
 * at all, so a root `onPreviewKeyEvent` that would put focus back never fires. That approach was
 * built, wired at the navigation host, and observed doing nothing; only preventing the escape
 * works. A handheld never exhibits any of this, because a finger grants focus again — a remote has
 * no equivalent gesture, so an unfocused app is an app with no input device, and the only exit is
 * force-stopping the process.
 *
 * `exit = Cancel` refuses a move that would leave this subtree, which makes `moveFocus` report
 * false and leaves focus exactly where it was.
 *
 * **This is a backstop, not the primary mechanism.** Every surface still sets its own initial focus,
 * and the one strand actually reproduced on a device was cured at its source — the composer refuses
 * a downward move outright (`ui/composer/`), because it is bottom-most and has no target there. If
 * this wrapper ever becomes load-bearing for ordinary navigation, that is a focus-order bug
 * upstream and belongs fixed there.
 */
// `FocusProperties.exit` is still experimental. Opted in deliberately: it is the only API that
// expresses "focus may not leave this subtree", and the alternative is provably unavailable.
@OptIn(ExperimentalComposeUiApi::class)
@Composable
internal fun DpadFocusContainer(content: @Composable () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .focusProperties { exit = { FocusRequester.Cancel } }
            .focusGroup(),
    ) {
        content()
    }
}
