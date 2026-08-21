package com.mewbo.aura.ui.common

import androidx.compose.ui.focus.FocusDirection
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.text.TextRange
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The escape table in [TextFieldFocusEscape], pinned arm by arm.
 *
 * Plain JVM: the decision is a pure function of a key, a selection and a length, which is the whole
 * reason it was pulled out of the modifier. The Compose half — that the modifier actually moves
 * focus out of the real composer — is [com.mewbo.aura.ui.composer.ComposerDpadEscapeTest]; this
 * suite would still pass if the modifier were never applied to anything, so the two are not
 * redundant.
 *
 * The negative arms carry the weight here. A rule that escapes on EVERY arrow is trivially
 * navigable and quietly destroys in-text editing: Left and Right would stop moving the caret, so a
 * user could never correct a typo in the middle of a draft. Those two arms are what stop a future
 * "just make it always escape" simplification from looking correct.
 */
class TextFieldFocusEscapeTest {

    @Test
    fun `up and down always leave the field`() {
        // Mid-text caret in a long draft — the case where a caret-movement rule would keep the key.
        val midDraft = TextRange(5)

        assertEquals(FocusDirection.Up, TextFieldFocusEscape.directionFor(Key.DirectionUp, midDraft, 20))
        assertEquals(FocusDirection.Down, TextFieldFocusEscape.directionFor(Key.DirectionDown, midDraft, 20))
    }

    @Test
    fun `left leaves the field only from the very start of the text`() {
        assertEquals(
            FocusDirection.Left,
            TextFieldFocusEscape.directionFor(Key.DirectionLeft, TextRange(0), textLength = 8),
        )
        assertNull(
            "a caret with text to its left must move the caret, not the focus",
            TextFieldFocusEscape.directionFor(Key.DirectionLeft, TextRange(1), textLength = 8),
        )
    }

    @Test
    fun `right leaves the field only from the very end of the text`() {
        assertEquals(
            FocusDirection.Right,
            TextFieldFocusEscape.directionFor(Key.DirectionRight, TextRange(8), textLength = 8),
        )
        assertNull(
            "a caret with text to its right must move the caret, not the focus",
            TextFieldFocusEscape.directionFor(Key.DirectionRight, TextRange(7), textLength = 8),
        )
    }

    /**
     * An empty draft is simultaneously at the start and at the end, so both horizontal arms fire —
     * this is the state the app COLD LAUNCHES into, and the one that trapped the remote.
     */
    @Test
    fun `an empty draft escapes in every direction`() {
        val empty = TextRange(0)

        assertEquals(FocusDirection.Left, TextFieldFocusEscape.directionFor(Key.DirectionLeft, empty, 0))
        assertEquals(FocusDirection.Right, TextFieldFocusEscape.directionFor(Key.DirectionRight, empty, 0))
        assertEquals(FocusDirection.Up, TextFieldFocusEscape.directionFor(Key.DirectionUp, empty, 0))
        assertEquals(FocusDirection.Down, TextFieldFocusEscape.directionFor(Key.DirectionDown, empty, 0))
    }

    /**
     * With a range selected, Left/Right mean "collapse the selection" — editing, not navigation —
     * so the field keeps them even at the text's boundaries, where a collapsed caret would escape.
     */
    @Test
    fun `a non-collapsed selection keeps the horizontal keys even at both boundaries`() {
        val whole = TextRange(0, 8)

        assertNull(TextFieldFocusEscape.directionFor(Key.DirectionLeft, whole, textLength = 8))
        assertNull(TextFieldFocusEscape.directionFor(Key.DirectionRight, whole, textLength = 8))
    }

    /**
     * The no-selection overload, taken by every field whose state is a plain `String`.
     *
     * Horizontal escape becomes unconditional here, and that is the whole difference — a caller
     * with no caret cannot answer "am I at the boundary", and the two ways of being wrong are not
     * symmetric: guessing "not at the boundary" re-traps the remote with no way out, guessing "at
     * the boundary" costs one line's worth of caret movement in a single-line field.
     */
    @Test
    fun `with no selection to consult every arrow leaves the field`() {
        assertEquals(FocusDirection.Up, TextFieldFocusEscape.directionFor(Key.DirectionUp))
        assertEquals(FocusDirection.Down, TextFieldFocusEscape.directionFor(Key.DirectionDown))
        assertEquals(FocusDirection.Left, TextFieldFocusEscape.directionFor(Key.DirectionLeft))
        assertEquals(FocusDirection.Right, TextFieldFocusEscape.directionFor(Key.DirectionRight))
    }

    /**
     * The arm that stops the coarse table from swallowing the two keys a television cannot spare:
     * BACK is its only way out of a screen, and OK is what
     * [com.mewbo.aura.ui.common.imeOnConfirmOnly] needs in order to raise the keyboard at all — an escape
     * claiming either would leave the field unusable rather than merely un-editable.
     */
    @Test
    fun `with no selection the non-arrow keys are still never an escape`() {
        assertNull(TextFieldFocusEscape.directionFor(Key.Back))
        assertNull(TextFieldFocusEscape.directionFor(Key.DirectionCenter))
        assertNull(TextFieldFocusEscape.directionFor(Key.Enter))
        assertNull(TextFieldFocusEscape.directionFor(Key.A))
    }

    /**
     * The two tables agree wherever the richer one has no extra information to use, which is what
     * makes the coarse one a genuine degradation of the same rule rather than a second rule that
     * could drift from it.
     */
    @Test
    fun `both tables agree on the vertical arms regardless of caret`() {
        val midDraft = TextRange(5)

        assertEquals(
            TextFieldFocusEscape.directionFor(Key.DirectionUp),
            TextFieldFocusEscape.directionFor(Key.DirectionUp, midDraft, 20),
        )
        assertEquals(
            TextFieldFocusEscape.directionFor(Key.DirectionDown),
            TextFieldFocusEscape.directionFor(Key.DirectionDown, midDraft, 20),
        )
    }

    @Test
    fun `keys that are not arrows are never an escape`() {
        val empty = TextRange(0)

        // Enter and Back are the two that would be most damaging to swallow: one sends the draft,
        // the other is a television's only way out of a screen.
        assertNull(TextFieldFocusEscape.directionFor(Key.Enter, empty, 0))
        assertNull(TextFieldFocusEscape.directionFor(Key.Back, empty, 0))
        assertNull(TextFieldFocusEscape.directionFor(Key.A, empty, 0))
        assertNull(TextFieldFocusEscape.directionFor(Key.DirectionCenter, empty, 0))
    }
}
