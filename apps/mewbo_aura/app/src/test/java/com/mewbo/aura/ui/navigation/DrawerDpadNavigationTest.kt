package com.mewbo.aura.ui.navigation

import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.assertIsNotFocused
import androidx.compose.ui.test.isFocused
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performKeyInput
import androidx.compose.ui.test.pressKey
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.device.DeviceShape
import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.ui.common.LocalDeviceShape
import com.mewbo.aura.ui.sessions.RecentsFilter
import com.mewbo.aura.ui.sessions.SessionsUiState
import com.mewbo.aura.ui.sessions.SessionsViewModel
import com.mewbo.aura.ui.settings.SettingsUiState
import com.mewbo.aura.ui.settings.SettingsViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.mockito.Mockito.mock
import org.mockito.Mockito.`when`
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The navigation content is operable by a D-pad remote — driven through the real
 * [AuraDrawerContent], because what is pinned here is a property of its composable ORDER and of
 * where focus may travel, neither of which a pure test can observe.
 *
 * **What was reported from a Fire TV, and why the shape below is the fix.** Settings lived in the
 * footer, AFTER an unbounded `LazyColumn`. A lazy list composes only what is visible, so
 * two-dimensional focus search had nothing to land on past the visible rows: pressing DOWN walked
 * the recents list, overshot Settings, and eventually left the drawer entirely for the chat composer
 * behind it — a text field that consumes all four arrows. On a television that is terminal, not
 * cosmetic: a remote has no gesture that grants focus back.
 *
 * **Every test enters by focus search from a neighbour above, never via the content's own initial
 * `FocusRequester`.** That is deliberate, and it is also a finding: under this harness the
 * request fires before the lazy row is placed, so the `runCatching` around it swallows a
 * `requestFocus` that never happens and nothing is focused at all. Entering by arrow is both the
 * more robust fixture and a truer model of a remote — and it makes the [NavigationHost] arms below
 * mean what they say, since entry is exactly what a closed sheet must refuse.
 *
 * The screen is configured TALL deliberately. Every assertion about a row being absent would also
 * pass if the row were merely scrolled out of the viewport, so the fixture is sized to compose the
 * whole list, and `a modal sheet lists every recent it was given` is the control proving that it does.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], qualifiers = "w960dp-h2000dp-xhdpi")
class DrawerDpadNavigationTest {

    @get:Rule
    val rule = createComposeRule()

    /**
     * The reported failure, stated as a number: Settings must be a SMALL bounded walk from the first
     * row a remote reaches, and the walk must not cross the recents list.
     *
     * The assertion is two-sided on purpose. "Focused after three" alone would still pass if a
     * fourth row were inserted above Settings and the count silently grew; "not focused after two"
     * is what makes the count exact, so any change to the top group's length reddens this.
     */
    @Test
    fun `in a persistent rail settings is three presses from the first row`() {
        setContent(DeviceShape.Television, NavigationHost.PersistentRail, sessions(12))
        enterFromAbove()

        repeat(2) { pressDown() }
        rule.onNodeWithText("Settings").assertIsNotFocused()

        pressDown()
        rule.onNodeWithText("Settings").assertIsFocused()
    }

    /**
     * The handheld half of the same law, and the acceptance bar for the whole change: the touch
     * drawer renders exactly as it did.
     *
     * Written against the two renderings' distinguishing artifact rather than a screenshot — the
     * footer's affordance is an `IconButton` whose glyph carries the accessible name, the hoisted
     * one is a labelled row whose glyph is decorative. So "Settings as TEXT" and "Settings as a
     * CONTENT DESCRIPTION" are mutually exclusive, and asserting both directions on both shapes
     * also pins that the action is never duplicated.
     */
    @Test
    fun `a modal sheet keeps settings in the footer and nowhere else`() {
        setContent(DeviceShape.Handheld, NavigationHost.ModalSheet(isOpen = true), sessions(3))

        rule.onNodeWithContentDescription("Settings").assertExists()
        rule.onNodeWithText("Settings").assertDoesNotExist()
    }

    @Test
    fun `a persistent rail renders settings once, as a row rather than the footer button`() {
        setContent(DeviceShape.Television, NavigationHost.PersistentRail, sessions(3))

        rule.onNodeWithText("Settings").assertExists()
        rule.onNodeWithContentDescription("Settings").assertDoesNotExist()
    }

    /**
     * The row set follows the SHELL, never the device — a television hosting a modal sheet still
     * gets the sheet's rows.
     *
     * This combination cannot occur in production, and that is exactly why it is worth pinning: the
     * rows once read `LocalDeviceShape` directly while also taking a [NavigationHost], so the two
     * could disagree and nothing in the type system noticed. Re-introducing that read turns this
     * green assertion red, and no other test in this file would move.
     */
    @Test
    fun `the row set follows the shell rather than the device`() {
        setContent(DeviceShape.Television, NavigationHost.ModalSheet(isOpen = true), sessions(3))

        rule.onNodeWithContentDescription("Settings").assertExists()
        rule.onNodeWithText("Settings").assertDoesNotExist()
    }

    /**
     * The control for `a persistent rail caps recents`, and it is not decoration: an absent row and a
     * virtualised row are indistinguishable to `assertDoesNotExist`, so without a shape that DOES
     * compose the eighth session that test would pass against a `LazyColumn` which merely ran out
     * of viewport. Same fixture, same screen, one difference.
     */
    @Test
    fun `a modal sheet lists every recent it was given`() {
        setContent(DeviceShape.Handheld, NavigationHost.ModalSheet(isOpen = true), sessions(8))

        rule.onNodeWithText("Chat 8").assertExists()
    }

    @Test
    fun `a persistent rail caps recents`() {
        setContent(DeviceShape.Television, NavigationHost.PersistentRail, sessions(8))

        rule.onNodeWithText("Chat 6").assertExists()
        rule.onNodeWithText("Chat 7").assertDoesNotExist()
    }

    /**
     * Focus may not leave an OPEN modal sheet, in the direction that actually escaped on the device.
     *
     * `isFocused()` still existing at the end is the control. "The node below is not focused" is
     * true both when containment worked and when focus was lost altogether — and losing it is the
     * worse of the two outcomes, since nothing on a remote can put it back.
     */
    @Test
    fun `focus cannot walk out of an open modal sheet`() {
        setContent(DeviceShape.Handheld, NavigationHost.ModalSheet(isOpen = true), sessions(4))
        enterFromAbove()

        repeat(30) { pressDown() }

        rule.onNodeWithTag(Below).assertIsNotFocused()
        rule.onNode(isFocused()).assertExists()
    }

    /**
     * The other half, and the one an unconditional `exit = Cancel` would have broken: a CLOSED sheet
     * is still composed, so it must refuse ENTRY rather than trap whoever wanders in.
     */
    @Test
    fun `focus cannot walk into a closed modal sheet`() {
        setContent(DeviceShape.Handheld, NavigationHost.ModalSheet(isOpen = false), sessions(4))

        rule.runOnIdle { aboveFocus.requestFocus() }
        rule.onNodeWithTag(Above).assertIsFocused()

        repeat(5) { pressDown() }

        rule.onNodeWithText("New chat").assertIsNotFocused()
    }

    /**
     * The rail's opposite requirement, and the reason containment is a property of the HOST rather
     * than of the device: a permanent rail must let focus OUT, because leaving it for the content
     * beside it is the only way to use the app from there. Containing it would strand the remote in
     * the navigation list — the same unrecoverable state, reached from the other direction.
     */
    @Test
    fun `focus can leave a persistent rail`() {
        setContent(DeviceShape.Television, NavigationHost.PersistentRail, sessions(2))
        enterFromAbove()

        repeat(30) { pressDown() }

        rule.onNodeWithTag(Below).assertIsFocused()
    }

    /** Walks in from the neighbour above and pins that the walk arrived where it claims to. */
    private fun enterFromAbove() {
        rule.runOnIdle { aboveFocus.requestFocus() }
        rule.onNodeWithTag(Above).assertIsFocused()

        pressDown()

        // THE CONTROL for every press count that follows: without it, a run where entry silently
        // failed would still satisfy a later "Settings is not focused" for entirely the wrong reason.
        rule.onNodeWithText("New chat").assertIsFocused()
    }

    private fun pressDown() = rule.onRoot().performKeyInput { pressKey(Key.DirectionDown) }

    private val aboveFocus = FocusRequester()

    private fun setContent(
        shape: DeviceShape,
        host: NavigationHost,
        sessions: List<SessionSummary>,
    ) {
        rule.setContent {
            CompositionLocalProvider(LocalDeviceShape provides shape) {
                Column {
                    Neighbour(Above, aboveFocus)
                    AuraDrawerContent(
                        currentSessionId = null,
                        host = host,
                        onNewChat = {},
                        onOpenSearch = {},
                        onOpenSession = {},
                        onOpenSettings = {},
                        onOpenApps = {},
                        modifier = Modifier.height(DrawerHeight),
                        sessionsViewModel = sessionsViewModel(sessions),
                        settingsViewModel = settingsViewModel(),
                    )
                    Neighbour(Below, null)
                }
            }
        }
    }

    /**
     * Stands in for the content the navigation sits beside. A real height matters: two-dimensional
     * focus search compares BOUNDS, so a zero-height neighbour is not a candidate in any direction
     * and a containment test built on one would pass without containing anything.
     */
    @Composable
    private fun Neighbour(tag: String, focusRequester: FocusRequester?) {
        androidx.compose.foundation.layout.Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(NeighbourHeight)
                .testTag(tag)
                .let { if (focusRequester != null) it.focusRequester(focusRequester) else it }
                .focusable(),
        )
    }

    /**
     * Both ViewModels are mocked rather than constructed. [SessionsViewModel] would be reachable
     * with one fake repository, but [SettingsViewModel] takes twelve collaborators to answer the one
     * question this content asks it — a display name — and a twelve-fake fixture would be a larger
     * surface to maintain than the behaviour under test.
     */
    private fun sessionsViewModel(sessions: List<SessionSummary>): SessionsViewModel {
        val vm = mock(SessionsViewModel::class.java)
        `when`(vm.uiState).thenReturn(MutableStateFlow(SessionsUiState.Loaded(sessions, offline = false)))
        `when`(vm.filter).thenReturn(MutableStateFlow(RecentsFilter.ALL))
        return vm
    }

    private fun settingsViewModel(): SettingsViewModel {
        val vm = mock(SettingsViewModel::class.java)
        `when`(vm.uiState).thenReturn(MutableStateFlow(SettingsUiState(displayName = "Alex")))
        return vm
    }

    /**
     * Newest-first, all today, none pinned — the ordinary rail. `updatedAt` carries the backend's
     * NUMERIC offset rather than a bare `Z`: `Timestamps.parseInstantOrNull` is what bucket
     * assignment runs through, and a fixture in the wrong shape would exercise its fallback leg
     * instead of the one a device actually feeds it.
     */
    private fun sessions(count: Int): List<SessionSummary> {
        val now = java.time.Instant.now().toString().removeSuffix("Z") + "+00:00"
        return (1..count).map { index ->
            SessionSummary(
                sessionId = "session-$index",
                title = "Chat $index",
                status = "idle",
                running = false,
                doneReason = null,
                origin = "mobile",
                recoverable = true,
                createdAt = now,
                updatedAt = now,
            )
        }
    }

    private companion object {
        const val Above = "above-the-drawer"
        const val Below = "below-the-drawer"
        val DrawerHeight = 1400.dp
        val NeighbourHeight = 150.dp
    }
}
