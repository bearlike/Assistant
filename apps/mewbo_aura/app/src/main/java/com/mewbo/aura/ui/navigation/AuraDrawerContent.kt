package com.mewbo.aura.ui.navigation

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
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
import androidx.compose.runtime.setValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.ui.chat.ChatIcons
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
 * [isOpen] drives a refresh-on-open (spec §6.7: "Refresh sessions when the drawer opens").
 */
@Composable
fun AuraDrawerContent(
    currentSessionId: String?,
    isOpen: Boolean,
    onNewChat: () -> Unit,
    onOpenSearch: () -> Unit,
    onOpenSession: (String) -> Unit,
    onOpenSettings: () -> Unit,
    onOpenApps: () -> Unit,
    modifier: Modifier = Modifier,
    sessionsViewModel: SessionsViewModel = hiltViewModel(),
    settingsViewModel: SettingsViewModel = hiltViewModel(),
) {
    LaunchedEffect(isOpen) {
        if (isOpen) sessionsViewModel.refresh()
    }
    val sessionsState by sessionsViewModel.uiState.collectAsStateWithLifecycle()
    val settingsState by settingsViewModel.uiState.collectAsStateWithLifecycle()
    val recentsFilter by sessionsViewModel.filter.collectAsStateWithLifecycle()

    // Long-press session-actions sheet (Rename/Archive) — non-null while a row's sheet is open.
    var actionTarget by remember { mutableStateOf<SessionSummary?>(null) }

    // Scope to the active filter (mobile-only by default), then bucket into Today / Previous 7 days
    // / Older. Both steps are pure + unit-tested (`RecentsFilter.matches`, `SessionGrouping`) and
    // recompute only when the fetched list or the active filter changes.
    val loaded = sessionsState as? SessionsUiState.Loaded
    val sections = remember(loaded?.sessions, recentsFilter) {
        loaded?.sessions
            ?.filter { recentsFilter.matches(it) }
            ?.let { SessionGrouping.group(it, Instant.now(), ZoneId.systemDefault()) }
            .orEmpty()
    }

    Column(modifier = modifier.fillMaxSize().background(AuraColors.surfaceDrawer)) {
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
            item {
                // Subtle section divider (side-rail visual-polish task, 2026-07-14): separates the
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
        // Subtle divider above the pinned footer (side-rail visual-polish task, 2026-07-14) — the
        // same hairline treatment as the action-rows/Recents divider above, closing the rail's
        // third section (Recents list vs. the settings/user-icon/username area).
        HorizontalDivider(
            color = AuraColors.outlineHairline,
            modifier = Modifier.padding(horizontal = AuraSpacing.screenGutter),
        )
        DrawerFooter(displayName = settingsState.displayName, onOpenSettings = onOpenSettings)
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
        )
    }
}

/**
 * "Recents" section header + the mobile/all scope filter (user directive 2026-07-03). The list
 * defaults to [RecentsFilter.MOBILE_ONLY]; the overflow menu flips to [RecentsFilter.ALL] —
 * mirroring the web console's origin-scoped session list, collapsed to a single binary toggle so
 * the rail stays uncluttered. This header is the stable anchor the date sub-dividers hang beneath.
 *
 * Side-rail visual-polish task, 2026-07-14: gained a leading [ChatIcons.Clock] glyph (reused
 * verbatim — an existing house glyph, not a new hand-roll; team-lead directive: keep it), sized/
 * tinted/spaced exactly like `SettingsScreen`'s own `SettingsSectionHeader`
 * (`AuraSpacing.DrawerRow.iconSize`/`iconToLabelGap`, `textSecondary` — "the glyph reads as one
 * hierarchy step below body content, same tier as the label beside it, never louder"), purely
 * decorative (`contentDescription = null`, the "Recents" text is the accessible name). The trigger
 * also swapped [Icons.Default.MoreVert] for [Icons.Default.FilterAlt] — a real funnel glyph reads
 * as "filter" where a generic overflow dot-stack doesn't. `FilterAlt` lands from
 * `material-icons-extended`, added to the dependency catalog 2026-07-14 (orchestrator, hard user
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
            IconButton(onClick = { menuOpen = true }) {
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
 * Compact session-history row (user directive 2026-07-03). Denser than the action rows
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
            .combinedClickable(onClick = onClick, onLongClick = onLongClick)
            .padding(horizontal = AuraSpacing.screenGutter / 2),
        verticalAlignment = Alignment.CenterVertically,
    ) {
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

@Composable
private fun DrawerFooter(displayName: String, onOpenSettings: () -> Unit, modifier: Modifier = Modifier) {
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
        IconButton(onClick = onOpenSettings) {
            Icon(Icons.Default.Settings, contentDescription = "Settings", tint = AuraColors.iconPrimary)
        }
    }
}
