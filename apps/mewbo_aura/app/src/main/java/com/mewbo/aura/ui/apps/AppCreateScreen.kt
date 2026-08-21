package com.mewbo.aura.ui.apps

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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.common.dpadFocusEscape
import com.mewbo.aura.ui.common.imeOnConfirmOnly
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * "New app" creation entry (design spec §5 flow 1): intent text + own/shared workspace toggle →
 * `POST /api/apps` → live build progress → navigates to [AppDetailScreen] on
 * [AppCreatePhase.Ready].
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppCreateScreen(
    onBack: () -> Unit,
    onAppReady: (String) -> Unit,
    modifier: Modifier = Modifier,
    viewModel: AppCreateViewModel = hiltViewModel(),
) {
    val phase by viewModel.phase.collectAsStateWithLifecycle()
    val projects by viewModel.projects.collectAsStateWithLifecycle()
    var intent by remember { mutableStateOf("") }
    var workspace by remember { mutableStateOf<AppWorkspaceChoice>(AppWorkspaceChoice.Own) }
    var projectMenuOpen by remember { mutableStateOf(false) }

    LaunchedEffect(phase) {
        val ready = phase as? AppCreatePhase.Ready ?: return@LaunchedEffect
        onAppReady(ready.appId)
    }

    val composing = phase is AppCreatePhase.Composing

    Scaffold(
        modifier = modifier,
        containerColor = AuraColors.surfaceCanvas,
        topBar = {
            TopAppBar(
                title = { Text("New app", style = AuraType.titleBar) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back", tint = AuraColors.iconPrimary)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = AuraColors.surfaceCanvas),
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(AuraSpacing.screenGutter),
        ) {
            Text("What should it do?", style = AuraType.sectionHeader)
            Spacer(Modifier.height(AuraSpacing.Composer.gapTight))
            OutlinedTextField(
                value = intent,
                onValueChange = { intent = it },
                enabled = composing,
                placeholder = { Text("e.g. Track my weekly reading list and remind me on Sundays") },
                minLines = 3,
                maxLines = 6,
                // Without these a remote that reaches the intent field can neither leave it nor see
                // the rest of the form behind the IME. Plain String state, so every arrow escapes.
                modifier = Modifier
                    .fillMaxWidth()
                    .dpadFocusEscape()
                    .imeOnConfirmOnly(),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedTextColor = AuraColors.textPrimary,
                    unfocusedTextColor = AuraColors.textPrimary,
                    cursorColor = AuraColors.accentPrimary,
                    focusedBorderColor = AuraColors.accentPrimary,
                    unfocusedBorderColor = AuraColors.outlineHairline,
                ),
            )

            Spacer(Modifier.height(AuraSpacing.Composer.internalPadding))
            Text("Workspace", style = AuraType.sectionHeader)
            Spacer(Modifier.height(AuraSpacing.Composer.gapTight))
            SingleChoiceSegmentedButtonRow(modifier = Modifier.fillMaxWidth()) {
                SegmentedButton(
                    selected = workspace is AppWorkspaceChoice.Own,
                    onClick = { if (composing) workspace = AppWorkspaceChoice.Own },
                    shape = SegmentedButtonDefaults.itemShape(index = 0, count = 2),
                    enabled = composing,
                ) { Text("Own workspace") }
                SegmentedButton(
                    selected = workspace is AppWorkspaceChoice.Shared,
                    onClick = {
                        if (!composing) return@SegmentedButton
                        viewModel.loadProjectsIfNeeded()
                        projectMenuOpen = true
                    },
                    shape = SegmentedButtonDefaults.itemShape(index = 1, count = 2),
                    enabled = composing,
                ) { Text("Shared workspace") }
            }
            Text(
                text = "An own workspace is a fresh, dedicated project just for this app. A shared workspace " +
                    "points the app at a project you already use, so it can read the same files and context.",
                style = AuraType.caption,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )

            // Gated on `projectMenuOpen` too, NOT just `workspace is Shared` — the ONLY place
            // `workspace` ever becomes `Shared` is a `DropdownMenuItem` click inside this very
            // block, so gating solely on `workspace is Shared` made the whole picker (and thus
            // "Shared workspace") permanently unreachable: the segmented button set
            // `projectMenuOpen = true` with nothing composed to read it. An intermediate "picking"
            // state (workspace still Own, menu open) is fine — dismissing without choosing reverts
            // to hidden, same as before ever opening it.
            if (workspace is AppWorkspaceChoice.Shared || projectMenuOpen) {
                Box {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(top = AuraSpacing.Composer.gapTight),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            text = (workspace as? AppWorkspaceChoice.Shared)?.project?.name ?: "Choose a project",
                            style = AuraType.listItem,
                            color = AuraColors.textPrimary,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.weight(1f),
                        )
                    }
                    DropdownMenu(expanded = projectMenuOpen, onDismissRequest = { projectMenuOpen = false }) {
                        val available = projects.orEmpty().filter { it.available }
                        if (available.isEmpty()) {
                            DropdownMenuItem(text = { Text("No projects available", style = AuraType.listItem) }, onClick = {})
                        }
                        available.forEach { project: ProjectSummary ->
                            DropdownMenuItem(
                                text = { Text(project.name, style = AuraType.listItem) },
                                onClick = {
                                    workspace = AppWorkspaceChoice.Shared(project)
                                    projectMenuOpen = false
                                },
                            )
                        }
                    }
                }
            }

            Spacer(Modifier.height(AuraSpacing.Composer.internalPadding))
            Button(
                onClick = { viewModel.submit(intent, workspace) },
                enabled = composing && intent.isNotBlank(),
                colors = ButtonDefaults.buttonColors(containerColor = AuraColors.accentPrimary),
                modifier = Modifier.fillMaxWidth(),
            ) { Text("Build") }

            Spacer(Modifier.height(AuraSpacing.Composer.internalPadding))
            AppCreateProgress(phase = phase, onRetry = viewModel::resetToComposing)
        }
    }
}

@Composable
private fun AppCreateProgress(phase: AppCreatePhase, onRetry: () -> Unit, modifier: Modifier = Modifier) {
    when (phase) {
        is AppCreatePhase.Composing -> Unit
        is AppCreatePhase.Submitting -> BuildingRow(text = "Starting the build…", modifier = modifier)
        is AppCreatePhase.Building -> BuildingRow(text = phase.narration ?: "Building your app…", modifier = modifier)
        is AppCreatePhase.Ready -> Unit // LaunchedEffect above navigates away immediately.
        is AppCreatePhase.Failed -> ErrorCard(reason = phase.message, retryLabel = "Try again", onRetry = onRetry, modifier = modifier)
    }
}

@Composable
private fun BuildingRow(text: String, modifier: Modifier = Modifier) {
    Row(modifier = modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        CircularProgressIndicator(
            color = AuraColors.accentPrimary,
            modifier = Modifier.size(AuraSpacing.Composer.iconSize),
        )
        Spacer(Modifier.width(AuraSpacing.Composer.internalPadding))
        Text(text = text, style = AuraType.caption, maxLines = 2, overflow = TextOverflow.Ellipsis)
    }
}
