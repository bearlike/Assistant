package com.mewbo.aura.ui.composer

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Extension
import androidx.compose.material.icons.filled.MoreHoriz
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.ComposerScope
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.model.ToolSummary
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraMotion
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

private sealed interface OptionsPane {
    data object Root : OptionsPane
    data object Project : OptionsPane
    data object Tools : OptionsPane
}

/**
 * The composer "+" sheet. Anatomy per the reference plus-menu/tools-sheet captures:
 * top = "Photos"/"Files" attach pills; below a divider, "Session" rows ([scope]'s Project/Tools)
 * each drill into an in-sheet sub-list (back-arrow header, mirrors the tools-sheet's
 * section-header -> row rhythm). [scope]'s catalogs are `null` until
 * [com.mewbo.aura.ui.chat.ChatViewModel.refreshComposerScope] resolves them - rows degrade to their
 * placeholder label ("Temporary" / "All tools") rather than crashing. [onRefresh] re-triggers that
 * same resolve on demand (Project/Tools pane headers), for a catalog that changed server-side
 * mid-session.
 *
 * Project/Tools are editable whenever no run is in flight - including on an already-created, IDLE
 * session. [runInFlight] freezes them ONLY while a turn is actively Sending/Streaming:
 * the backend re-resolves both from THAT running query's own request body, so mutating them mid-run
 * would desync the live turn - but between turns there is no live run to desync, and the resend-context
 * mechanism carries the new pick into the NEXT turn (mirrors the freely re-pickable ModelPickerSheet).
 * While frozen the rows show their current value with a "set for this chat" subtitle instead of a
 * drill-in affordance.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ComposerOptionsSheet(
    scope: ComposerScope,
    runInFlight: Boolean,
    visionSupported: Boolean,
    onDismiss: () -> Unit,
    onPhotosTap: () -> Unit,
    onFilesTap: () -> Unit,
    onSelectProject: (String?) -> Unit,
    onToggleTool: (String) -> Unit,
    onToggleServer: (List<String>, Boolean) -> Unit,
    onRefresh: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var pane by remember { mutableStateOf<OptionsPane>(OptionsPane.Root) }

    ModalBottomSheet(
        onDismissRequest = { pane = OptionsPane.Root; onDismiss() },
        sheetState = rememberModalBottomSheetState(),
        containerColor = AuraColors.surfaceInput,
        shape = SheetShape,
        modifier = modifier,
    ) {
        when (pane) {
            OptionsPane.Root -> RootPane(
                scope = scope,
                runInFlight = runInFlight,
                visionSupported = visionSupported,
                onPhotosTap = onPhotosTap,
                onFilesTap = onFilesTap,
                onOpenProject = { if (!runInFlight) pane = OptionsPane.Project },
                onOpenTools = { if (!runInFlight) pane = OptionsPane.Tools },
            )
            OptionsPane.Project -> ProjectPane(
                projects = scope.projects.orEmpty(),
                selectedKey = scope.selectedProjectKey,
                onBack = { pane = OptionsPane.Root },
                onSelect = { key -> onSelectProject(key); pane = OptionsPane.Root },
                onRefresh = onRefresh,
            )
            OptionsPane.Tools -> ToolsPane(
                scope = scope,
                onBack = { pane = OptionsPane.Root },
                onToggle = onToggleTool,
                onToggleServer = onToggleServer,
                onRefresh = onRefresh,
            )
        }
    }
}

@Composable
private fun RootPane(
    scope: ComposerScope,
    runInFlight: Boolean,
    visionSupported: Boolean,
    onPhotosTap: () -> Unit,
    onFilesTap: () -> Unit,
    onOpenProject: () -> Unit,
    onOpenTools: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxWidth().padding(bottom = AuraSpacing.Composer.internalPadding)) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.internalPadding),
        ) {
            AttachPillButton(
                icon = ChatIcons.PhotoGlyph,
                label = "Photos",
                enabled = visionSupported,
                caption = "Needs a vision model".takeIf { !visionSupported },
                onClick = onPhotosTap,
                modifier = Modifier.weight(1f),
            )
            AttachPillButton(
                icon = ChatIcons.FileGlyph,
                label = "Files",
                enabled = true,
                caption = null,
                onClick = onFilesTap,
                modifier = Modifier.weight(1f),
            )
        }

        HorizontalDivider(color = AuraColors.outlineHairline)

        Text(
            text = "Session",
            style = AuraType.sectionHeader,
            modifier = Modifier.padding(
                horizontal = AuraSpacing.screenGutter,
                vertical = AuraSpacing.DrawerRow.sectionHeaderTopPad / 2,
            ),
        )
        DrillInRow(label = "Project", value = scope.projectDisplayName, frozen = runInFlight, onClick = onOpenProject)
        DrillInRow(label = "Tools", value = scope.toolsSummaryLabel, frozen = runInFlight, onClick = onOpenTools)
    }
}

@Composable
private fun AttachPillButton(
    icon: ImageVector,
    label: String,
    enabled: Boolean,
    caption: String?,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
            modifier = Modifier
                .fillMaxWidth()
                .background(AuraColors.surfaceSelected, AuraShape.radiusPill)
                .clickable(enabled = enabled, onClick = onClick)
                .padding(horizontal = AuraSpacing.Composer.internalPadding, vertical = AuraSpacing.Composer.gapTight),
        ) {
            Icon(
                imageVector = icon,
                contentDescription = null,
                tint = if (enabled) AuraColors.iconPrimary else AuraColors.textTertiary,
                modifier = Modifier.size(AuraSpacing.Composer.iconSize),
            )
            Text(
                text = label,
                style = AuraType.listItem,
                color = if (enabled) AuraColors.textPrimary else AuraColors.textTertiary,
            )
        }
        if (caption != null) {
            Text(
                text = caption,
                style = AuraType.caption,
                modifier = Modifier.padding(top = AttachPillCaptionGap, start = AuraSpacing.Composer.gapTight),
            )
        }
    }
}

@Composable
private fun DrillInRow(label: String, value: String, frozen: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(enabled = !frozen, onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
            Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary)
            Text(
                text = if (frozen) "$value — set for this chat" else value,
                style = AuraType.caption,
            )
        }
        if (!frozen) {
            Icon(
                imageVector = Icons.AutoMirrored.Filled.KeyboardArrowRight,
                contentDescription = null,
                tint = AuraColors.textSecondary,
                modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
            )
        }
    }
}

/** Shared drill-in pane header. [onRefresh], when non-null, renders a trailing refresh action
 * (Root's own header is inline and unaffected - this composable is never used there). */
@Composable
private fun PaneHeader(title: String, onBack: () -> Unit, modifier: Modifier = Modifier, onRefresh: (() -> Unit)? = null) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .padding(horizontal = AuraSpacing.Composer.gapTight),
    ) {
        Icon(
            imageVector = Icons.AutoMirrored.Filled.ArrowBack,
            contentDescription = "Back",
            tint = AuraColors.iconPrimary,
            modifier = Modifier
                .clickable(onClick = onBack)
                .padding(AuraSpacing.Composer.gapTight)
                .size(AuraSpacing.DrawerRow.iconSize),
        )
        Text(
            text = title,
            style = AuraType.listItem,
            color = AuraColors.textPrimary,
            modifier = Modifier
                .weight(1f)
                .padding(start = AuraSpacing.DrawerRow.iconToLabelGap),
        )
        if (onRefresh != null) {
            // IconButton's own touch target is already 48dp - no explicit size token needed.
            IconButton(onClick = onRefresh) {
                Icon(
                    imageVector = Icons.Filled.Refresh,
                    contentDescription = "Refresh",
                    tint = AuraColors.iconPrimary,
                    modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
                )
            }
        }
    }
}

/** Bottom clearance shared by both scrollable panes so the last row never sits under the
 * gesture/nav bar: the system inset plus the same breathing room [RootPane] already applies at its
 * own bottom edge. */
@Composable
private fun paneContentPadding(): PaddingValues {
    val navBarInset = WindowInsets.navigationBars.asPaddingValues().calculateBottomPadding()
    return PaddingValues(bottom = navBarInset + AuraSpacing.Composer.internalPadding)
}

@Composable
private fun ProjectPane(
    projects: List<ProjectSummary>,
    selectedKey: String?,
    onBack: () -> Unit,
    onSelect: (String?) -> Unit,
    onRefresh: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxWidth()) {
        PaneHeader(title = "Project", onBack = onBack, onRefresh = onRefresh)
        // weight(fill = false): capped at the sheet's remaining height (never squeezed/overlapped
        // past it), but wraps to content when shorter - a short project list doesn't force the
        // sheet to full height.
        LazyColumn(modifier = Modifier.weight(1f, fill = false), contentPadding = paneContentPadding()) {
            item(key = TemporaryProjectKey) {
                ProjectRow(label = "Temporary", temporary = true, selected = selectedKey == null, onClick = { onSelect(null) })
            }
            // Divider BELOW the ephemeral temp-dir cwd, setting it apart from real, saved projects
            // (user directive 2026-07-14) — same treatment as the settings ProjectPickerSheet.
            item(key = TemporaryDividerKey) {
                HorizontalDivider(color = AuraColors.outlineHairline)
            }
            items(projects, key = { it.contextKey }) { project ->
                ProjectRow(label = project.name, temporary = false, selected = selectedKey == project.contextKey, onClick = { onSelect(project.contextKey) })
            }
        }
    }
}

@Composable
private fun ProjectRow(label: String, temporary: Boolean, selected: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        // Leading scope glyph: the ephemeral "date_range" for the temporary scratch cwd (muted tint),
        // the "project" glyph (scopeProject accent) for a real project. A FILLED slot on every row,
        // so this is not the §7.13 empty-icon-slot indent regression.
        Icon(
            imageVector = if (temporary) ChatIcons.TemporaryProjectScope else ChatIcons.ProjectScope,
            contentDescription = null,
            tint = if (temporary) AuraColors.textSecondary else AuraColors.scopeProject,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(modifier = Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary, modifier = Modifier.weight(1f))
        if (selected) {
            Icon(
                imageVector = Icons.Filled.Check,
                contentDescription = "Selected",
                tint = AuraColors.accentPrimary,
                modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
            )
        }
    }
}

/**
 * Tools pane (renamed back from "MCP Servers" - the catalog now legitimately mixes
 * MCP servers with capability-gated product-tool groups like "Wiki"/"Agentic Search", see
 * [com.mewbo.aura.data.repo.SessionScopeRepository.tools]; "MCP Servers" stopped being accurate the
 * moment a non-MCP group could appear here). [ToolSummary.groupKey] groups collapse to one
 * switch-bearing header row each (stable alphabetical order), expanding to their member [ToolRow]s
 * on tap - the catalog runs 300+ entries, so both the grouping and the [LazyColumn] below are
 * load-bearing, not cosmetic. [expanded] is local, transient UI state (per review:
 * persisting it beyond this pane's own composition isn't part of the brief).
 *
 * Provenance sections (user directive 2026-07-14): the server/product groups are LAYERED under
 * [ScopeSectionHeader]s by [ToolSummary.scope] (project → system → plugin → builtin → other,
 * [ComposerScope.FACET_ORDER]) — an accessible, TalkBack-navigable heading per provenance, with the
 * existing per-server expand/collapse + bulk toggle untouched beneath it. Ordering is scope-section
 * first, then alphabetical within a section (the prior stable order).
 */
@Composable
private fun ToolsPane(
    scope: ComposerScope,
    onBack: () -> Unit,
    onToggle: (String) -> Unit,
    onToggleServer: (List<String>, Boolean) -> Unit,
    onRefresh: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val ordered = scope.tools.orEmpty().groupBy { it.groupKey }.entries
        .sortedWith(compareBy({ scopeSectionRank(sectionScopeOf(it.value)) }, { it.key }))
    val expanded = remember { mutableStateMapOf<String, Boolean>() }

    Column(modifier = modifier.fillMaxWidth()) {
        PaneHeader(title = "Tools", onBack = onBack, onRefresh = onRefresh)
        LazyColumn(modifier = Modifier.weight(1f, fill = false), contentPadding = paneContentPadding()) {
            var lastSection: String? = null
            ordered.forEach { (server, tools) ->
                val section = sectionScopeOf(tools)
                if (section != lastSection) {
                    lastSection = section
                    item(key = "section:$section") { ScopeSectionHeader(scope = section) }
                }
                val (active, total) = scope.activeCountFor(server)
                item(key = "header:$server") {
                    ServerGroupHeader(
                        server = server,
                        active = active,
                        total = total,
                        expanded = expanded[server] == true,
                        onToggleExpand = { expanded[server] = expanded[server] != true },
                        onToggleAll = { checked -> onToggleServer(tools.map { it.toolId }, checked) },
                    )
                }
                if (expanded[server] == true) {
                    items(tools, key = { it.toolId }) { tool ->
                        ToolRow(
                            tool = tool,
                            active = scope.isToolActive(tool.toolId),
                            onToggle = { onToggle(tool.toolId) },
                            // Gutter-inset convention (ui/CLAUDE.md chat rendering rules): an extra
                            // screenGutter on top of ToolRow's own, indenting group members under
                            // their header.
                            modifier = Modifier.padding(start = AuraSpacing.screenGutter),
                        )
                    }
                }
                item(key = "divider:$server") {
                    HorizontalDivider(color = AuraColors.outlineHairline)
                }
            }
        }
    }
}

/** Provenance section bucket for a server/product group — its first tool's recognized
 * [ToolSummary.scope], or [ComposerScope.FACET_OTHER] when the backend omitted it (tools in one
 * group share an origin, so a single representative is honest). */
private fun sectionScopeOf(tools: List<ToolSummary>): String =
    tools.firstNotNullOfOrNull { it.scope }?.takeIf { it in ComposerScope.FACET_ORDER } ?: ComposerScope.FACET_OTHER

private val SCOPE_SECTION_ORDER: List<String> = ComposerScope.FACET_ORDER + ComposerScope.FACET_OTHER

private fun scopeSectionRank(scope: String): Int =
    SCOPE_SECTION_ORDER.indexOf(scope).let { if (it < 0) SCOPE_SECTION_ORDER.size else it }

private fun scopeSectionLabel(scope: String): String = when (scope) {
    "project" -> "Project"
    "system" -> "System"
    "plugin" -> "Plugin"
    "builtin" -> "Built-in"
    else -> "Other"
}

private fun scopeSectionIcon(scope: String): ImageVector = when (scope) {
    "project" -> ChatIcons.ProjectScope // folder / workspace
    "system" -> Icons.Filled.Settings // gear
    "plugin" -> Icons.Filled.Extension // puzzle piece — the canonical plugin glyph
    "builtin" -> ChatIcons.ToolScope // wrench (core tooling)
    else -> Icons.Filled.MoreHoriz // misc / unlabeled
}

/**
 * Provenance section header in the Tools pane (user directive 2026-07-14): an icon + capitalized
 * scope label ("Project" / "System" / …), marked as a semantic HEADING so TalkBack announces it and
 * lets the user jump between provenance sections. Quiet chrome (sectionHeader type + textSecondary),
 * distinct from the interactive [ServerGroupHeader] rows beneath it.
 *
 * Spacing: asymmetric, top-heavy - full [AuraSpacing.DrawerRow.sectionHeaderTopPad]
 * above, no explicit gap below - the same rhythm [com.mewbo.aura.ui.navigation.AuraDrawerContent]'s
 * `RecentsHeader` and `SettingsScreen`'s section headers already use (and the same "headings cling to
 * what follows" ~2:1 shape as [AuraSpacing.Markdown.headingTopGap]/`headingBottomGap`). The prior
 * `sectionHeaderTopPad / 2` SYMMETRIC padding (still correct for the one-off, non-repeating headers in
 * `ActionSheet.SheetHeader`/`ModelPickerSheet`) put this header exactly as close to the PREVIOUS
 * section's last row as to its own content beneath it, so a provenance boundary read with the same
 * weight as an ordinary intra-section row gap - the reported "boundaries unclear" bug. [ServerGroupHeader]
 * and [ToolRow] are still bare fixed-height rows with zero explicit inter-row gap, so this header's own
 * top/bottom asymmetry is what carries the whole boundary signal.
 */
@Composable
private fun ScopeSectionHeader(scope: String, modifier: Modifier = Modifier) {
    val label = scopeSectionLabel(scope)
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
        modifier = modifier
            .fillMaxWidth()
            .padding(
                start = AuraSpacing.screenGutter,
                top = AuraSpacing.DrawerRow.sectionHeaderTopPad,
                end = AuraSpacing.screenGutter,
            )
            .semantics(mergeDescendants = true) { heading() },
    ) {
        Icon(
            imageVector = scopeSectionIcon(scope),
            contentDescription = null, // the label beside it carries the meaning for TalkBack
            tint = AuraColors.textSecondary,
            modifier = Modifier.size(AuraSpacing.Composer.scopeRowIconSize),
        )
        Text(text = label, style = AuraType.sectionHeader, color = AuraColors.textSecondary)
    }
}

/** One tool group's (an MCP server, or a product-tool group like "Wiki") collapsed/expandable row:
 * name + "N of M on" caption, a switch that bulk
 * toggles every tool in the group, and a chevron that rotates to indicate expansion. Tapping the
 * row (not the switch) toggles [expanded] - the switch's own tap is consumed by its own toggleable
 * modifier first, so it never also fires [onToggleExpand]. */
@Composable
private fun ServerGroupHeader(
    server: String,
    active: Int,
    total: Int,
    expanded: Boolean,
    onToggleExpand: () -> Unit,
    onToggleAll: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
) {
    val chevronRotation by animateFloatAsState(
        targetValue = if (expanded) 90f else 0f,
        animationSpec = AuraMotion.composerMorphSpring,
        label = "server-group-chevron",
    )
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onToggleExpand)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
            // The per-server provenance tag moved UP to the enclosing
            // [ScopeSectionHeader] (user directive 2026-07-14) — showing it here too would
            // double-label every row, so the group row is now just its name + "N of M on".
            Text(text = server, style = AuraType.listItem, color = AuraColors.textPrimary)
            Text(text = "$active of $total on", style = AuraType.caption)
        }
        Switch(checked = active > 0, onCheckedChange = onToggleAll)
        Icon(
            imageVector = Icons.AutoMirrored.Filled.KeyboardArrowRight,
            contentDescription = null,
            tint = AuraColors.textSecondary,
            modifier = Modifier
                .padding(start = AuraSpacing.Composer.gapTight)
                .size(AuraSpacing.DrawerRow.iconSize)
                .graphicsLayer { rotationZ = chevronRotation },
        )
    }
}

@Composable
private fun ToolRow(tool: ToolSummary, active: Boolean, onToggle: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
            Text(text = tool.name, style = AuraType.listItem, color = AuraColors.textPrimary)
            tool.disabledReason?.let { Text(text = it, style = AuraType.caption) }
        }
        Switch(checked = active, onCheckedChange = { onToggle() })
    }
}

/** Top corners only (same shape [com.mewbo.aura.ui.chat.ModelPickerSheet] uses). */
private val SheetShape = RoundedCornerShape(topStart = AuraShape.radiusBubble, topEnd = AuraShape.radiusBubble)

/** No matching [AuraSpacing] token for the disabled-Photos caption's gap; flagged in the task
 * report (same convention `ui/chat/ChatScreen.kt`'s `TitleGap`/`ChevronSize` already established). */
private val AttachPillCaptionGap: Dp = 4.dp

/** [ProjectPane]'s synthetic "Temporary" row has no [ProjectSummary.contextKey] to key off - a
 * sentinel unlikely to collide with a real one. */
private const val TemporaryProjectKey = "__temporary__"

/** LazyColumn key for the divider that sets the ephemeral "Temporary" row apart from real projects. */
private const val TemporaryDividerKey = "__temporary_divider__"
