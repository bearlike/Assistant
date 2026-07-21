package com.mewbo.aura.ui.apps

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mewbo.aura.data.model.AppFreshness
import com.mewbo.aura.data.model.AppSummary
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.sessions.RelativeTime
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Apps gallery (design spec §4D): cards showing title, icon emoji, status, freshness. Peer surface
 * to Search chats/Settings — pushed WITHOUT the drawer, same "drawer only on home" pattern
 * (`ui/navigation/CLAUDE.md`).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppsGalleryScreen(
    onBack: () -> Unit,
    onOpenApp: (String) -> Unit,
    onCreateApp: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: AppsViewModel = hiltViewModel(),
) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()

    Scaffold(
        modifier = modifier,
        containerColor = AuraColors.surfaceCanvas,
        topBar = {
            TopAppBar(
                title = { Text("Apps", style = AuraType.titleBar) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back", tint = AuraColors.iconPrimary)
                    }
                },
                actions = {
                    IconButton(onClick = onCreateApp) {
                        Icon(Icons.Filled.Add, contentDescription = "New app", tint = AuraColors.iconPrimary)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = AuraColors.surfaceCanvas),
            )
        },
    ) { padding ->
        when (val state = uiState) {
            is AppsUiState.Loading -> Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center,
            ) {
                CircularProgressIndicator(color = AuraColors.accentPrimary)
            }
            is AppsUiState.Error -> Box(modifier = Modifier.fillMaxSize().padding(padding).padding(AuraSpacing.screenGutter)) {
                ErrorCard(reason = state.message, onRetry = viewModel::refresh)
            }
            is AppsUiState.Loaded -> if (state.apps.isEmpty()) {
                EmptyAppsNote(onCreateApp = onCreateApp, modifier = Modifier.fillMaxSize().padding(padding))
            } else {
                LazyColumn(
                    modifier = Modifier.fillMaxSize().padding(padding),
                    contentPadding = PaddingValues(
                        horizontal = AuraSpacing.screenGutter,
                        vertical = AuraSpacing.Composer.internalPadding,
                    ),
                    verticalArrangement = Arrangement.spacedBy(AuraSpacing.AppCard.cardGap),
                ) {
                    items(state.apps, key = { it.appId }) { app ->
                        AppGalleryCard(app = app, freshness = state.freshness[app.appId], onClick = { onOpenApp(app.appId) })
                    }
                }
            }
        }
    }
}

@Composable
private fun AppGalleryCard(
    app: AppSummary,
    freshness: AppFreshness?,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(AuraShape.radiusThumb))
            .border(AuraShape.hairlineWidth, AuraColors.outlineHairline, RoundedCornerShape(AuraShape.radiusThumb))
            .clickable(onClick = onClick)
            .padding(AuraSpacing.Composer.internalPadding),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(AuraSpacing.AppCard.iconSlot), contentAlignment = Alignment.Center) {
            Text(text = app.icon.ifBlank { DEFAULT_APP_ICON }, style = AuraType.toolCardDisplay)
        }
        Spacer(Modifier.width(AuraSpacing.Composer.gapTight))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = app.title.ifBlank { "Untitled app" },
                style = AuraType.listItem,
                color = AuraColors.textPrimary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (app.summary.isNotBlank()) {
                Text(app.summary, style = AuraType.caption, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                AppStatusDot(status = app.status)
                Spacer(Modifier.width(AuraSpacing.DrawerRow.runningDotGap))
                Text(text = freshnessLabel(freshness), style = AuraType.caption, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
    }
}

/** Status dot + label — the same trailing-dot liveness idiom `RecentSessionRow` uses for
 * `SessionSummary.running`, reused here for an app's [AppSummary.status] (compact, no new chip
 * primitive per the compact design language). */
@Composable
internal fun AppStatusDot(status: String, modifier: Modifier = Modifier) {
    val color = statusColor(status)
    Row(modifier = modifier, verticalAlignment = Alignment.CenterVertically) {
        Box(
            modifier = Modifier
                .size(AuraSpacing.DrawerRow.runningDotSize)
                .clip(CircleShape)
                .background(color),
        )
        Spacer(Modifier.width(AuraSpacing.DrawerRow.runningDotGap))
        Text(text = status.ifBlank { "unknown" }, style = AuraType.caption, color = color)
    }
}

private fun statusColor(status: String) = when (status) {
    "live" -> AuraColors.accentPrimary
    "broken", "failed" -> AuraColors.accentError
    else -> AuraColors.textSecondary // draft, building, paused, archived
}

/** [freshness] `null` reads as "never run yet" — same for a genuinely-fresh app and a fetch that
 * failed (see [AppsViewModel.loadFreshness]'s KDoc for why those two are deliberately
 * indistinguishable to a card). [AppFreshness.stale] takes precedence over a bare "succeeded" read
 * so a card never paints a false-green for data that's actually overdue or failed to refresh. */
internal fun freshnessLabel(freshness: AppFreshness?): String {
    if (freshness == null || freshness.lastSuccessAt == null) return "Not yet run"
    val relative = RelativeTime.format(freshness.lastSuccessAt)
    return when {
        freshness.lastRunStatus == "running" -> "Running…"
        freshness.stale || freshness.lastRunStatus == "failed" -> "Stale — last updated $relative"
        else -> "Updated $relative"
    }
}

@Composable
private fun EmptyAppsNote(onCreateApp: () -> Unit, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier.padding(AuraSpacing.screenGutter),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("No apps yet", style = AuraType.listItem, color = AuraColors.textPrimary)
        Spacer(Modifier.height(AuraSpacing.Composer.gapTight))
        Text("Describe what you want and Mewbo builds it.", style = AuraType.caption)
        Spacer(Modifier.height(AuraSpacing.Composer.internalPadding))
        Button(onClick = onCreateApp) { Text("New app") }
    }
}

private const val DEFAULT_APP_ICON = "📦" // package emoji fallback — the spec's `icon` field is agent-authored and may be blank
