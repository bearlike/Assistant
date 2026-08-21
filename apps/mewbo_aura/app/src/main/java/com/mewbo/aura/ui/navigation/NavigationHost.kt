package com.mewbo.aura.ui.navigation

/**
 * The chrome [AuraDrawerContent] is mounted in — the ONE difference between the two navigation
 * shells, expressed as data so the content composable does not have to ask what device it is on.
 *
 * The rows are shared; only the frame around them differs. A handheld reveals navigation on demand
 * over the content, because screen width is scarce and a thumb can summon it. A television keeps it
 * permanently on screen, because a remote has no summoning gesture and 960dp of width is not
 * scarce. Both facts below follow from that one difference, which is why they are members here
 * rather than two booleans a caller could pass inconsistently.
 */
sealed interface NavigationHost {

    /** Whether the content is on screen and should refresh its sessions and claim initial focus. */
    val isActive: Boolean

    /**
     * Whether focus must be prevented from crossing this content's boundary.
     *
     * **True only for a modal sheet, and the distinction is load-bearing in both directions.** A
     * sheet composed while closed still sits in the focus graph, so a remote can walk into rows the
     * user cannot see; a sheet that is open sits over content whose rows must not be reachable
     * through it. A permanent rail has neither problem and must NOT be contained — focus moving
     * right into the transcript is the primary way the rail is used, and refusing that exit would
     * strand the remote in the navigation list with no way into the app.
     */
    val containsFocus: Boolean

    /**
     * Whether every row must be reachable by walking DOWN a bounded list.
     *
     * True for a shell a D-pad traverses one row at a time, and it has two consequences the rows
     * apply themselves: Settings moves out of the pinned footer into the top group, and recents is
     * capped. Both exist for the one reason — a footer sits AFTER a `LazyColumn`, which composes
     * only what is visible, so focus search has nothing to land on past the visible rows and
     * overshoots whatever follows them. A thumb has neither problem: it touches what it can see.
     *
     * This is a member rather than a `LocalDeviceShape` read inside the rows because the shell
     * already encodes the answer. Asking the device a second time lets the two disagree — a rail
     * rendering footer-only Settings, unreachable, with nothing in the type system to catch it.
     */
    val walksRowsLinearly: Boolean

    /** A `ModalNavigationDrawer` sheet: revealed by the top bar's menu button, dismissed by the scrim. */
    data class ModalSheet(val isOpen: Boolean) : NavigationHost {
        override val isActive: Boolean get() = isOpen
        override val containsFocus: Boolean get() = true
        override val walksRowsLinearly: Boolean get() = false
    }

    /** An always-visible rail beside the content. Never opens or closes, so it is always active. */
    data object PersistentRail : NavigationHost {
        override val isActive: Boolean = true
        override val containsFocus: Boolean = false
        override val walksRowsLinearly: Boolean = true
    }
}
