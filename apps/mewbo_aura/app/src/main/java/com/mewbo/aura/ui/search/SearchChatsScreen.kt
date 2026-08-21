package com.mewbo.aura.ui.search

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.style.TextOverflow
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.common.dpadFocusEscape
import com.mewbo.aura.ui.common.imeOnConfirmOnly
import com.mewbo.aura.ui.sessions.RelativeTime
import com.mewbo.aura.ui.sessions.SessionsUiState
import com.mewbo.aura.ui.sessions.SessionsViewModel
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Search chats (spec §6.8): magnifier + inline text field, `outlineHairline` divider, `Recent`
 * section header, rows filtered live over [SessionsViewModel]'s cached list on every keystroke
 * ([SessionSearchFilter] — pure, unit-tested separately). Empty query shows the full recents list.
 */
@Composable
fun SearchChatsScreen(
    onOpenSession: (String) -> Unit,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: SessionsViewModel = hiltViewModel(),
) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    var query by rememberSaveable { mutableStateOf("") }

    Column(modifier = modifier.fillMaxSize().background(AuraColors.surfaceCanvas)) {
        SearchTopRow(
            query = query,
            onQueryChange = { query = it },
            onClear = { query = "" },
            onBack = onBack,
        )
        HorizontalDivider(color = AuraColors.outlineHairline)
        Text(
            text = "Recent",
            style = AuraType.sectionHeader,
            modifier = Modifier.padding(
                horizontal = AuraSpacing.screenGutter,
                vertical = AuraSpacing.Composer.internalPadding,
            ),
        )
        when (val state = uiState) {
            is SessionsUiState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = AuraColors.accentPrimary)
            }

            is SessionsUiState.Error -> Box(
                modifier = Modifier.fillMaxSize().padding(AuraSpacing.screenGutter),
                contentAlignment = Alignment.Center,
            ) {
                ErrorCard(reason = state.message, onRetry = viewModel::refresh)
            }

            is SessionsUiState.Loaded -> SearchResults(
                sessions = state.sessions,
                query = query,
                onOpenSession = onOpenSession,
                modifier = Modifier.fillMaxSize(),
            )
        }
    }
}

@Composable
private fun SearchResults(
    sessions: List<SessionSummary>,
    query: String,
    onOpenSession: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val filtered = remember(sessions, query) { SessionSearchFilter.filter(sessions, query) }
    if (filtered.isEmpty()) {
        Box(modifier.padding(AuraSpacing.screenGutter), contentAlignment = Alignment.Center) {
            Text(
                text = if (query.isBlank()) "No chats yet" else "No matches",
                style = AuraType.bodyMessage,
                color = AuraColors.textSecondary,
            )
        }
        return
    }
    LazyColumn(modifier) {
        items(filtered, key = { it.sessionId }) { session ->
            SearchResultRow(session = session, onClick = { onOpenSession(session.sessionId) })
        }
    }
}

@Composable
private fun SearchTopRow(
    query: String,
    onQueryChange: (String) -> Unit,
    onClear: () -> Unit,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.internalPadding),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            imageVector = Icons.Default.Search,
            contentDescription = null,
            tint = AuraColors.iconPrimary,
        )
        Spacer(Modifier.width(AuraSpacing.Composer.gapTight))
        Box(modifier = Modifier.weight(1f)) {
            if (query.isEmpty()) {
                Text(text = "Search for chats", style = AuraType.listItem, color = AuraColors.textSecondary)
            }
            BasicTextField(
                value = query,
                onValueChange = onQueryChange,
                textStyle = AuraType.listItem.copy(color = AuraColors.textPrimary),
                singleLine = true,
                cursorBrush = SolidColor(AuraColors.accentPrimary),
                // The search field is the first thing a remote lands on, and without these it is
                // also the last: the arrows move the caret and the IME swallows BACK, so the
                // results below are unreachable. Plain String state, so no caret to consult.
                modifier = Modifier
                    .fillMaxWidth()
                    .dpadFocusEscape()
                    .imeOnConfirmOnly(),
            )
        }
        // Trailing affordance per spec §6.8: clear (✕) while there's a query, back when empty —
        // the magnifier above is always the leading glyph, this is the only icon that swaps.
        IconButton(onClick = if (query.isEmpty()) onBack else onClear) {
            if (query.isEmpty()) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back", tint = AuraColors.iconPrimary)
            } else {
                Icon(imageVector = Icons.Default.Close, contentDescription = "Clear", tint = AuraColors.iconPrimary)
            }
        }
    }
}

@Composable
private fun SearchResultRow(session: SessionSummary, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.searchRowHeight)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
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
        Spacer(Modifier.width(AuraSpacing.Composer.internalPadding))
        Text(text = RelativeTime.formatShort(session.updatedAt), style = AuraType.metaTrailing)
    }
}
