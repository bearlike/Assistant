package com.mewbo.aura.ui.navigation

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.focusGroup
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Apps
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.FilterAlt
import androidx.compose.material.icons.filled.PushPin
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.withFrameNanos
import androidx.compose.runtime.setValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusProperties
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.common.auraFocusRing
import com.mewbo.aura.ui.sessions.RecentsFilter
import com.mewbo.aura.ui.sessions.SessionGrouping
import com.mewbo.aura.ui.sessions.SessionsUiState
import com.mewbo.aura.ui.sessions.SessionsViewModel
import com.mewbo.aura.ui.sessions.matches
import com.mewbo.aura.ui.settings.SettingsViewModel
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import java.time.Instant
import java.time.ZoneId

/**
 * Drawer content (spec §6.7, corrected by Rev E §E-6 against on-device frame F2): wordmark, New
 * chat / Search chats rows, Recents, pinned footer. [AuraNavHost] owns the `ModalNavigationDrawer`
 * mechanics (width, scrim, open/close) — this composable is pure content.
 *
 * The `surfaceSelected` pill tracks the CURRENT location (Rev E-6): [currentSessionId] `null` means
 * a fresh chat is open (New chat row selected), otherwise the matching recents row is selected.
 *
 * **These rows serve BOTH navigation shells** — the handheld modal sheet and the television's
 * permanent rail — because the row vocabulary is the same on either and only the frame differs.
 * [host] carries every consequence of that frame — [NavigationHost.isActive],
 * [NavigationHost.containsFocus] and [NavigationHost.walksRowsLinearly] — and this composable never
 * asks what device it is on, because the shell already answered. `isActive` drives the refresh
 * (spec §6.7: "Refresh sessions when the drawer opens").
 *
 * **Two things change under [NavigationHost.walksRowsLinearly], and both are about a remote's
 * ONE-dimensional reach.** A finger touches Settings wherever it sits; a D-pad has to walk there,
 * and everything between the first row and it is a corridor.
 *
 * - **Settings is hoisted into the top action group** and the footer's icon button drops, so exactly
 *   one Settings affordance exists in either shape. In the footer it sat AFTER an unbounded
 *   `LazyColumn`, which composes only what is visible — so two-dimensional focus search into
 *   not-yet-composed recents rows overshot it entirely and Settings was not reachable at all.
 * - **Recents is capped at [LinearRowsRecentsCap]**, because a corridor of N rows between the top
 *   group and nothing else is still a corridor. The full list stays reachable through Search chats.
 *
 * Focus CONTAINMENT belongs to the modal sheet alone, and it must act in both directions.
 * `ModalNavigationDrawer` composes its sheet while closed too, so an unconditional `exit = Cancel`
 * would trap a remote inside an invisible drawer — strictly worse than an escaping one, and on a
 * television unrecoverable (`ui/common/DpadFocusContainer`, which records why recovering after the
 * fact cannot work). Open refuses to let focus leave; closed refuses to let it enter. A permanent
 * rail is contained in neither direction: moving right into the transcript is how it is used.
 */
// `FocusProperties.enter`/`exit` are still experimental. Opted in for the same reason
// `DpadFocusContainer` does: they are the only API expressing "focus may not cross this boundary".
@OptIn(ExperimentalComposeUiApi::class)
@Composable
fun AuraDrawerContent(
    currentSessionId: String?,
    host: NavigationHost,
    onNewChat: () -> Unit,
    onOpenSearch: () -> Unit,
    onOpenSession: (String) -> Unit,
    onOpenSettings: () -> Unit,
    onOpenApps: () -> Unit,
    modifier: Modifier = Modifier,
    sessionsViewModel: SessionsViewModel = hiltViewModel(),
    settingsViewModel: SettingsViewModel = hiltViewModel(),
) {
    LaunchedEffect(host.isActive) {
        if (host.isActive) sessionsViewModel.refresh()
    }
    // First D-pad press must land somewhere: without an initial focus target the drawer opens
    // with nothing focused, so the opening presses are silently swallowed. `runCatching` guards
    // against requesting focus on a node this recomposition hasn't attached to layout yet.
    //
    // A permanent rail claims focus once, on first composition, and never again — it is always
    // active, so re-requesting would yank focus back out of the transcript on every recomposition.
    val firstRowFocusRequester = remember { FocusRequester() }
    LaunchedEffect(host.isActive) {
        if (!host.isActive) return@LaunchedEffect
        // RETRY ACROSS FRAMES, and the retry is the whole point rather than defensive padding.
        // The first row lives in a `LazyColumn`, which places its children during layout — after
        // this effect first runs. A single request therefore throws "not attached", `runCatching`
        // swallows it, and the surface comes up with NOTHING focused: measured on a device at TV
        // geometry, where the opening D-pad press was silently eaten and only the second one moved
        // anything. Re-asking on each of the next few frames costs nothing once it lands, because
        // the loop returns the moment the request succeeds.
        repeat(InitialFocusAttempts) {
            if (runCatching { firstRowFocusRequester.requestFocus() }.isSuccess) {
                return@LaunchedEffect
            }
            withFrameNanos { }
        }
    }
    // Read from the SHELL, never from the device: `ChatHomeDestination` already made that choice and
    // a second read is a second chance to disagree with it.
    val walksRowsLinearly = host.walksRowsLinearly
    val sessionsState by sessionsViewModel.uiState.collectAsStateWithLifecycle()
    val settingsState by settingsViewModel.uiState.collectAsStateWithLifecycle()
    val recentsFilter by sessionsViewModel.filter.collectAsStateWithLifecycle()

    // Long-press session-actions sheet (Rename/Archive) — non-null while a row's sheet is open.
    var actionTarget by remember { mutableStateOf<SessionSummary?>(null) }

    // Scope to the active filter (mobile-only by default), then bucket into Today / Previous 7 days
    // / Older. Both steps are pure + unit-tested (`RecentsFilter.matches`, `SessionGrouping`) and
    // recompute only when the fetched list or the active filter changes.
    val loaded = sessionsState as? SessionsUiState.Loaded
    val sections = remember(loaded?.sessions, recentsFilter, walksRowsLinearly) {
        loaded?.sessions
            ?.filter { recentsFilter.matches(it) }
            // A pin is the user saying "keep this one in reach", so the television cap is applied to
            // the unpinned remainder only — capping the raw list could hide the very row a pin exists
            // to keep visible.
            ?.let { rows ->
                if (!walksRowsLinearly) {
                    rows
                } else {
                    val (pinned, rest) = rows.partition { it.pinned }
                    pinned + rest.take(LinearRowsRecentsCap)
                }
            }
            ?.let { SessionGrouping.group(it, Instant.now(), ZoneId.systemDefault()) }
            .orEmpty()
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .focusProperties {
                // Closed refuses ENTRY, open refuses EXIT. Both halves are load-bearing: the sheet
                // is composed either way, so one without the other just moves the trap.
                val contained = host.containsFocus && host.isActive
                enter = { if (host.containsFocus && !host.isActive) FocusRequester.Cancel else FocusRequester.Default }
                exit = { if (contained) FocusRequester.Cancel else FocusRequester.Default }
            }
            .focusGroup()
            .testTag(DrawerRootTag)
            .background(AuraColors.surfaceDrawer),
    ) {
        LazyColumn(modifier = Modifier.weight(1f)) {
            item {
                Text(
                    text = "Mewbo",
                    style = AuraType.wordmark,
                    modifier = Modifier.padding(
                        horizontal = AuraSpacing.screenGutter,
                        vertical = AuraSpacing.Composer.internalPadding,
                    ),
                )
            }
            item {
                DrawerRow(
                    label = "New chat",
                    selected = currentSessionId == null,
                    onClick = onNewChat,
                    modifier = Modifier.focusRequester(firstRowFocusRequester),
                    leading = { Icon(Icons.Default.Edit, contentDescription = null, tint = AuraColors.iconPrimary) },
                )
            }
            item {
                DrawerRow(
                    label = "Search chats",
                    selected = false,
                    onClick = onOpenSearch,
                    leading = { Icon(Icons.Default.Search, contentDescription = null, tint = AuraColors.iconPrimary) },
                )
            }
            item {
                // Mewbo Apps (design spec §4D) — a peer nav destination to Search chats, pushed
                // WITHOUT the drawer once opened (same "drawer only on home" pattern as Search).
                DrawerRow(
                    label = "Apps",
                    selected = false,
                    onClick = onOpenApps,
                    leading = { Icon(Icons.Default.Apps, contentDescription = null, tint = AuraColors.iconPrimary) },
                )
            }
            if (walksRowsLinearly) {
                item {
                    // Hoisted out of the footer, which sits AFTER the lazy recents list and is
                    // therefore unreachable by focus search on a remote. Same `DrawerRow` and the
                    // same Filled glyph the footer used — one row shape, one icon weight.
                    DrawerRow(
                        label = "Settings",
                        selected = false,
                        onClick = onOpenSettings,
                        leading = { Icon(Icons.Default.Settings, contentDescription = null, tint = AuraColors.iconPrimary) },
                    )
                }
            }
            item {
                // Subtle section divider (side-rail visual-polish task): separates the
                // New chat / Search chats action rows from the Recents list below, same hairline
                // token DESIGN.md §4 names for ALL dividers. RecentsHeader still carries its own
                // sectionHeaderTopPad above "Recents" itself, so this stays a tight inset rather than
                // stacking a second large gap on top of that one.
                HorizontalDivider(
                    color = AuraColors.outlineHairline,
                    modifier = Modifier.padding(
                        horizontal = AuraSpacing.screenGutter,
                        vertical = AuraSpacing.Composer.gapTight,
                    ),
                )
            }
            item { RecentsHeader(filter = recentsFilter, onFilterChange = sessionsViewModel::setFilter) }
            when (val state = sessionsState) {
                is SessionsUiState.Loading -> item { DrawerInlineNote(text = "Loading…") }
                is SessionsUiState.Error -> item { DrawerInlineNote(text = state.message) }
                is SessionsUiState.Loaded -> {
                    if (sections.isEmpty()) {
                        item {
                            DrawerInlineNote(
                                text = if (recentsFilter == RecentsFilter.MOBILE_ONLY) {
                                    "No mobile chats yet."
                                } else {
                                    "No chats yet."
                                },
                            )
                        }
                    } else {
                        sections.forEach { section ->
                            item(key = "section-${section.header.name}") {
                                DateSubheader(label = section.header.label)
                            }
                            items(section.sessions, key = { it.sessionId }) { session ->
                                RecentSessionRow(
                                    session = session,
                                    selected = session.sessionId == currentSessionId,
                                    onClick = { onOpenSession(session.sessionId) },
                                    onLongClick = { actionTarget = session },
                                )
                            }
                        }
                    }
                }
            }
        }
        // On a television the footer keeps the account line — dropping it would lose information
        // that has no other home in the rail — but not a second Settings affordance; it renders at
        // all only while it still has something to show.
        val footerSettings = onOpenSettings.takeUnless { walksRowsLinearly }
        if (footerSettings != null || settingsState.displayName.isNotBlank()) {
            // Subtle divider above the pinned footer (side-rail visual-polish task) — the
            // same hairline treatment as the action-rows/Recents divider above, closing the rail's
            // third section (Recents list vs. the settings/user-icon/username area).
            HorizontalDivider(
                color = AuraColors.outlineHairline,
                modifier = Modifier.padding(horizontal = AuraSpacing.screenGutter),
            )
            DrawerFooter(displayName = settingsState.displayName, onOpenSettings = footerSettings)
        }
    }

    actionTarget?.let { target ->
        SessionActionsSheet(
            session = target,
            onDismiss = { actionTarget = null },
            onRename = { title, onResult -> sessionsViewModel.renameSession(target.sessionId, title, onResult) },
            onArchive = { onResult ->
                sessionsViewModel.archiveSession(target.sessionId) { success ->
                    // Archiving the OPEN session leaves nothing to show here — land on a fresh
                    // chat via the SAME callback the New chat row uses (it already closes the
                    // drawer too, see AuraNavHost's wrapping). A non-open session's row just
                    // disappears via the repository's own cache mutation; no navigation needed.
                    if (success && target.sessionId == currentSessionId) onNewChat()
                    onResult(success)
                }
            },
            onSetPinned = { pinned, onResult ->
                sessionsViewModel.setPinned(target.sessionId, pinned, onResult)
            },
        )
    }
}

/**
 * "Recents" section header + the mobile/all scope filter (user directive). The list
 * defaults to [RecentsFilter.MOBILE_ONLY]; the overflow menu flips to [RecentsFilter.ALL] —
 * mirroring the web console's origin-scoped session list, collapsed to a single binary toggle so
 * the rail stays uncluttered. This header is the stable anchor the date sub-dividers hang beneath.
 *
 * Side-rail visual-polish task: gained a leading [ChatIcons.Clock] glyph (reused
 * verbatim — an existing house glyph, not a new hand-roll; team-lead directive: keep it), sized/
 * tinted/spaced exactly like `SettingsScreen`'s own `SettingsSectionHeader`
 * (`AuraSpacing.DrawerRow.iconSize`/`iconToLabelGap`, `textSecondary` — "the glyph reads as one
 * hierarchy step below body content, same tier as the label beside it, never louder"), purely
 * decorative (`contentDescription = null`, the "Recents" text is the accessible name). The trigger
 * also swapped [Icons.Default.MoreVert] for [Icons.Default.FilterAlt] — a real funnel glyph reads
 * as "filter" where a generic overflow dot-stack doesn't. `FilterAlt` lands from
 * `material-icons-extended`, added to the dependency catalog (orchestrator, hard user
 * directive: real Material icons only, never hand-rolled vector paths) specifically because
 * `material-icons-core` carries no filter glyph at all — this was originally a hand-ported
 * `filter_alt` path (verbatim official SVG data) before the dependency landed; now deleted in
 * favor of the real library icon. `Icons.Default.*` (== `Icons.Filled.*`), matching every other
 * icon already in this drawer (Edit/Search/Settings/Check) — not the outlined family, so the
 * rail's icon language stays one weight.
 */
@Composable
private fun RecentsHeader(
    filter: RecentsFilter,
    onFilterChange: (RecentsFilter) -> Unit,
    modifier: Modifier = Modifier,
) {
    var menuOpen by remember { mutableStateOf(false) }
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(
                top = AuraSpacing.DrawerRow.sectionHeaderTopPad,
                start = AuraSpacing.screenGutter,
                end = AuraSpacing.screenGutter / 2,
            ),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            imageVector = ChatIcons.Clock,
            contentDescription = null,
            tint = AuraColors.textSecondary,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Text(
            text = "Recents",
            style = AuraType.sectionHeader,
            // TalkBack heading-navigation anchor (accessibility pass, side-rail visual-polish task) -
            // scoped to the text itself, not the whole Row, so the filter IconButton beside it keeps
            // its own independent, unmerged semantics node.
            modifier = Modifier.weight(1f).semantics { heading() },
        )
        Box {
            IconButton(onClick = { menuOpen = true }, modifier = Modifier.auraFocusRing(shape = CircleShape)) {
                Icon(Icons.Default.FilterAlt, contentDescription = "Filter sessions", tint = AuraColors.textSecondary)
            }
            DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                FilterMenuItem(label = "Mobile", selected = filter == RecentsFilter.MOBILE_ONLY) {
                    onFilterChange(RecentsFilter.MOBILE_ONLY)
                    menuOpen = false
                }
                FilterMenuItem(label = "All", selected = filter == RecentsFilter.ALL) {
                    onFilterChange(RecentsFilter.ALL)
                    menuOpen = false
                }
            }
        }
    }
}

@Composable
private fun FilterMenuItem(label: String, selected: Boolean, onClick: () -> Unit) {
    DropdownMenuItem(
        text = { Text(label, style = AuraType.listItem, color = AuraColors.textPrimary) },
        onClick = onClick,
        leadingIcon = {
            if (selected) {
                Icon(Icons.Default.Check, contentDescription = null, tint = AuraColors.accentPrimary)
            } else {
                Box(Modifier.size(AuraSpacing.DrawerRow.iconSize))
            }
        },
    )
}

/**
 * Lightweight date-bucket divider ("Today" / "Previous 7 days" / "Older"). One step down in BOTH
 * size and colour tier from the "Recents" header (DESIGN.md §1.1 hierarchy law), flush at
 * [AuraSpacing.screenGutter] — the same left edge as the header and the rows below it.
 */
@Composable
private fun DateSubheader(label: String, modifier: Modifier = Modifier) {
    Text(
        text = label,
        style = AuraType.caption,
        color = AuraColors.textTertiary,
        modifier = modifier.padding(
            top = AuraSpacing.DrawerRow.dateGroupTopPad,
            start = AuraSpacing.screenGutter,
            end = AuraSpacing.screenGutter,
        ),
    )
}

/**
 * Compact session-history row (user directive). Denser than the action rows
 * ([AuraSpacing.DrawerRow.recentRowHeight] vs [AuraSpacing.DrawerRow.height]) and — crucially —
 * with the title flush at [AuraSpacing.screenGutter], aligned with the "Recents" header and date
 * dividers. The prior `DrawerRow` reserved a 24dp leading icon slot that indented every recents
 * title ~36dp past the header — the indentation this directive removed. Running-session liveness
 * is therefore a TRAILING accent dot, never a leading one, so it can't re-introduce that indent.
 * Gated on [SessionSummary.running] (the wire's real liveness signal), never `status`.
 *
 * Long-press ([onLongClick]) opens the row's actions sheet (Rename/Archive) — `combinedClickable`'s
 * own `hapticFeedbackEnabled` (default `true` on this Compose Foundation version) fires the one
 * long-press haptic; nothing here fires a second. No leading icon/indent (DESIGN.md §7.13) — the
 * row's visual anatomy is unchanged by adding long-press.
 */
@Composable
private fun RecentSessionRow(
    session: SessionSummary,
    selected: Boolean,
    onClick: () -> Unit,
    onLongClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter / 2)
            .height(AuraSpacing.DrawerRow.recentRowHeight)
            .clip(AuraShape.radiusPill)
            .background(if (selected) AuraColors.surfaceSelected else Color.Transparent)
            .auraFocusRing(shape = AuraShape.radiusPill)
            .combinedClickable(onClick = onClick, onLongClick = onLongClick)
            .padding(horizontal = AuraSpacing.screenGutter / 2),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // Quiet leading marker — the row's own section header ("Pinned") already says why it's
        // here, so this stays subdued (textTertiary, compact) rather than competing with the
        // trailing running-dot's accentPrimary, which signals something actionable (a live run).
        if (session.pinned) {
            Icon(
                imageVector = Icons.Filled.PushPin,
                contentDescription = null,
                tint = AuraColors.textTertiary,
                modifier = Modifier.size(PinnedMarkerSize),
            )
            Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap / 2))
        }
        Text(
            text = session.title?.takeIf { it.isNotBlank() } ?: "Untitled session",
            style = AuraType.listItem,
            color = AuraColors.textPrimary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        if (session.running) {
            Spacer(Modifier.width(AuraSpacing.DrawerRow.runningDotGap))
            Box(
                Modifier
                    .size(AuraSpacing.DrawerRow.runningDotSize)
                    .clip(CircleShape)
                    .background(AuraColors.accentPrimary),
            )
        }
    }
}

/** Shared row shape for both the New chat/Search chats action rows and the recents list. */
@Composable
private fun DrawerRow(
    label: String,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    leading: @Composable () -> Unit,
) {
    Row(
        // No vertical padding before .height(): that would inflate the row past the spec's 56dp
        // (padding applies outside a fixed-height box) and add a gap frame F2 doesn't show -
        // reference rows are contiguous. Only the horizontal pill inset lives here.
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter / 2)
            .height(AuraSpacing.DrawerRow.height)
            .clip(AuraShape.radiusPill)
            .background(if (selected) AuraColors.surfaceSelected else Color.Transparent)
            .auraFocusRing(shape = AuraShape.radiusPill)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter / 2),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(AuraSpacing.DrawerRow.iconSize), contentAlignment = Alignment.Center) { leading() }
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Text(
            text = label,
            style = AuraType.listItem,
            color = AuraColors.textPrimary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
    }
}

@Composable
private fun DrawerInlineNote(text: String, modifier: Modifier = Modifier) {
    Text(
        text = text,
        style = AuraType.caption,
        modifier = modifier.padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight),
    )
}

/**
 * The pinned account line. [onOpenSettings] is nullable purely so the television shape can drop the
 * icon button without a second footer composable — on a remote Settings is a top-group row instead,
 * and rendering both would be two affordances for one destination.
 */
@Composable
private fun DrawerFooter(displayName: String, onOpenSettings: (() -> Unit)?, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.internalPadding),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (displayName.isNotBlank()) {
            // Spec §6.14: "32dp avatar" — no AuraSpacing token names this size yet (flagged in
            // the task report, same gap as the recents running-dot above).
            Box(
                modifier = Modifier.size(32.dp).clip(CircleShape).background(AuraColors.surfaceSelected),
                contentAlignment = Alignment.Center,
            ) {
                Text(text = displayName.first().uppercaseChar().toString(), style = AuraType.listItem, color = AuraColors.textPrimary)
            }
            Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
            Text(
                text = displayName,
                style = AuraType.listItem,
                color = AuraColors.textPrimary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
        } else {
            Spacer(Modifier.weight(1f))
        }
        if (onOpenSettings != null) {
            IconButton(onClick = onOpenSettings, modifier = Modifier.auraFocusRing(shape = CircleShape)) {
                Icon(Icons.Default.Settings, contentDescription = "Settings", tint = AuraColors.iconPrimary)
            }
        }
    }
}

/** No `AuraSpacing` token covers a recents-row inline marker glyph; same documented gap as
 * `SessionActionsSheet`'s `ConfirmSpinnerSize`. */
private val PinnedMarkerSize = 14.dp

/**
 * How many UNPINNED recents a linearly-walked shell lists. Not a design number — a reach budget:
 * every row here is one more D-pad press between the top action group and anything below the list,
 * and the rail is not the only way to a chat. Whatever the cap hides is one row away through the
 * Search chats row above it.
 */
private const val LinearRowsRecentsCap = 6

/** Focus-containment probe anchor — the subtree an open drawer must not let focus leave. */
internal const val DrawerRootTag = "aura-drawer-root"

/** How many frames the first row's focus request may be re-tried for. A `LazyColumn` places its
 * children a frame or two after composition, and a request made before that throws rather than
 * queuing; four is comfortably past observed placement while still bounded, so a row that never
 * arrives cannot spin. */
private const val InitialFocusAttempts = 4
