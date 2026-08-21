package com.mewbo.aura.ui.common

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusDirection
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.TextRange

/**
 * Which way focus should LEAVE a focused text field for a given D-pad key — or `null` to let the
 * field keep the key and move its own caret.
 *
 * **This is the single reason a remote could not drive this app at all.** A focused Compose text
 * field consumes every arrow key to move the caret, and it consumes them whether or not the caret
 * can actually go anywhere. On a handset that is invisible: a finger moves focus, so nothing is
 * ever trapped. On a television the composer takes focus on the first frame and the four arrow keys
 * are the ENTIRE input device, so the app opened onto a text field it was impossible to leave —
 * measured at 16:9 as eight consecutive arrow presses with the focused node never changing.
 *
 * The rule below is the standard television compromise, and it is a genuine trade, not a free win:
 *
 * | Key | Leaves the field | What is given up |
 * |---|---|---|
 * | Up / Down | always | caret movement between WRAPPED lines of a multi-line draft |
 * | Left | only with a collapsed caret at offset 0 | nothing |
 * | Right | only with a collapsed caret at the end | nothing |
 *
 * Up/Down is unconditional because a wrapped draft has no reliable "am I on the last line" signal
 * at this layer, and the alternative failure is unbounded: a two-line draft would re-trap the
 * remote with no way out. Left/Right stay conditional because the caret's own boundary IS that
 * signal, so within-text editing survives intact. A non-collapsed selection always keeps the key —
 * there, Left/Right mean "collapse the selection", which is editing, not navigation.
 *
 * Pure and Compose-free apart from the value classes in its signature, so the whole table above is
 * unit-testable without composing a text field or driving a key event.
 */
internal object TextFieldFocusEscape {

    /** O(1). [textLength] is the draft's length, not its capacity. */
    fun directionFor(key: Key, selection: TextRange, textLength: Int): FocusDirection? = when (key) {
        Key.DirectionLeft ->
            FocusDirection.Left.takeIf { selection.collapsed && selection.start == 0 }
        Key.DirectionRight ->
            FocusDirection.Right.takeIf { selection.collapsed && selection.end == textLength }
        else -> directionFor(key)
    }

    /**
     * The same table for a field that holds a plain `String` and so has NO selection to consult:
     * every arrow leaves, horizontal included.
     *
     * O(1). This is a strictly worse trade than the overload above and is taken only where the
     * better one is unavailable. A caller with no caret information cannot honestly answer "is the
     * caret at the boundary", and the two ways of being wrong are not symmetric: guessing "not at
     * the boundary" re-traps the remote, which is the unbounded failure the whole file exists to
     * prevent, while guessing "at the boundary" costs only within-text horizontal caret movement in
     * a single-line field — where Up/Down already leave and the caret has one line to travel.
     * That is the same argument the Up/Down arm above makes, applied one axis further.
     *
     * **Do not "fix" a caller onto the richer overload by converting its `String` state to a
     * `TextFieldValue`.** That moves selection ownership into the field and reintroduces
     * cursor-jump on every external state change — a worse regression than the one being traded.
     */
    fun directionFor(key: Key): FocusDirection? = when (key) {
        Key.DirectionUp -> FocusDirection.Up
        Key.DirectionDown -> FocusDirection.Down
        Key.DirectionLeft -> FocusDirection.Left
        Key.DirectionRight -> FocusDirection.Right
        else -> null
    }
}

/**
 * Lets a D-pad escape this text field, per [TextFieldFocusEscape].
 *
 * Apply to the modifier handed to the text field itself so it sits ABOVE the field's own key
 * handling in the chain — a preview event travels root-downward, so a modifier placed here is
 * offered the key before the field's caret logic claims it.
 *
 * **The key is forwarded to the field whenever focus could not actually move.**
 * [androidx.compose.ui.focus.FocusManager.moveFocus] returns `false` when nothing lies that way,
 * and returning that value verbatim is what makes the trade in [TextFieldFocusEscape] safe at the
 * edges of a layout: an unconditional Up with nothing above it falls back to caret movement rather
 * than swallowing the key into a dead end.
 *
 * Only `KeyDown` is inspected. The matching `KeyUp` is deliberately not consumed — by then focus
 * has already left, so the field never sees it.
 */
@Composable
internal fun Modifier.dpadFocusEscape(selection: TextRange, textLength: Int): Modifier =
    dpadEscape { key -> TextFieldFocusEscape.directionFor(key, selection, textLength) }

/**
 * The no-selection form, for a field whose state is a plain `String` — every arrow leaves, per
 * [TextFieldFocusEscape.directionFor]. Everything the overload above documents applies unchanged;
 * only the table it consults is the coarser one, and the trade it makes is documented there.
 */
@Composable
internal fun Modifier.dpadFocusEscape(): Modifier = dpadEscape(TextFieldFocusEscape::directionFor)

/** The one key-handling body both forms share; they differ only in which table they ask. */
@Composable
private fun Modifier.dpadEscape(directionFor: (Key) -> FocusDirection?): Modifier {
    val focusManager = LocalFocusManager.current
    return onPreviewKeyEvent { event ->
        if (event.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
        val direction = directionFor(event.key) ?: return@onPreviewKeyEvent false
        focusManager.moveFocus(direction)
    }
}
