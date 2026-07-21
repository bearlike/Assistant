package com.mewbo.aura.ui.settings

import android.Manifest
import android.content.Intent
import android.provider.Settings as SystemSettings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.AddCircle
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.Phone
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
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
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mewbo.aura.data.device.DeviceToolToggles
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.chat.ModelPickerSheet
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.common.LocalNoticeController
import com.mewbo.aura.ui.navigation.IS_DEBUG_BUILD
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * Settings (spec §6.14): server URL, API key, name, voice/motion prefs, default-assistant
 * shortcut. Rows read like `ui/navigation`'s drawer/search rows (fixed-height, contiguous, no
 * card chrome) rather than the M3 `ListItem`/`OutlinedTextField` defaults this screen used before
 * the compaction pass. Connection fields (base URL, API key) are the one exception to
 * immediate-apply: they hold local draft state and only reach [com.mewbo.aura.data.settings.SettingsStore] via the explicit
 * "Validate & save" pill, which probes them live first ([SettingsViewModel.validateAndSave]).
 *
 * Rows are grouped into named sections (Connection · Identity · Defaults · Voice & Motion ·
 * Device capabilities · Widgets · Debug), each opened by a [SettingsSectionHeader] — a leading
 * Material glyph plus label, marked as an a11y heading so TalkBack's heading-navigation gesture
 * can jump section to section. Purely presentational: no row's persistence key, callback, or
 * gating logic changed by this grouping.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    onOpenOrbGallery: () -> Unit,
    onOpenLivenessGallery: () -> Unit,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: SettingsViewModel = hiltViewModel(),
) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val noticeController = LocalNoticeController.current
    var projectPickerOpen by remember { mutableStateOf(false) }
    // two independent model-default pickers (app vs assist overlay), each reusing
    // the chat ModelPickerSheet + lazy catalog load, mirroring the project picker's shape above.
    var appModelPickerOpen by remember { mutableStateOf(false) }
    var overlayModelPickerOpen by remember { mutableStateOf(false) }

    LaunchedEffect(projectPickerOpen) {
        if (projectPickerOpen) viewModel.loadProjectsIfNeeded { noticeController.show(it) }
    }
    LaunchedEffect(appModelPickerOpen, overlayModelPickerOpen) {
        if (appModelPickerOpen || overlayModelPickerOpen) viewModel.loadModelsIfNeeded { noticeController.show(it) }
    }
    // Live OS read, not a persisted preference (task brief) - re-checked on first composition and
    // again once the request dialog below returns a result.
    LaunchedEffect(Unit) { viewModel.refreshSmsAccessStatus() }
    val smsPermissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        viewModel.refreshSmsAccessStatus()
    }

    Scaffold(
        modifier = modifier,
        containerColor = AuraColors.surfaceCanvas,
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
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
                .fillMaxWidth()
                .padding(padding)
                .verticalScroll(rememberScrollState()),
        ) {
            SettingsSectionHeader("Connection", icon = Icons.Filled.Lock)
            ConnectionFields(
                uiState = uiState,
                onValidateAndSave = { baseUrl, apiKey ->
                    viewModel.validateAndSave(baseUrl, apiKey) { modelCount ->
                        val plural = if (modelCount == 1) "" else "s"
                        noticeController.show("Connected — $modelCount model$plural available")
                    }
                },
                onSaveAnyway = viewModel::saveAnyway,
                onDismissError = viewModel::dismissConnectionError,
            )

            HorizontalDivider(color = AuraColors.outlineHairline)

            SettingsSectionHeader("Identity", icon = Icons.Filled.Person)
            NameField(displayName = uiState.displayName, onCommit = viewModel::setDisplayName)

            HorizontalDivider(color = AuraColors.outlineHairline)

            SettingsSectionHeader("Defaults", icon = Icons.Filled.Star)
            CompactRow(
                label = "Default project",
                modifier = Modifier.clickable { projectPickerOpen = true },
                trailing = {
                    Text(
                        text = resolveProjectDisplayName(uiState.selectedProject, uiState.projects),
                        style = AuraType.caption,
                    )
                },
            )
            CompactRow(
                label = "Default model — app",
                modifier = Modifier.clickable { appModelPickerOpen = true },
                trailing = {
                    Text(
                        text = resolveModelDisplayName(uiState.appDefaultModel, uiState.models),
                        style = AuraType.caption,
                    )
                },
            )
            CompactRow(
                label = "Default model — assistant overlay",
                modifier = Modifier.clickable { overlayModelPickerOpen = true },
                trailing = {
                    Text(
                        text = resolveModelDisplayName(uiState.overlayDefaultModel, uiState.models),
                        style = AuraType.caption,
                    )
                },
            )

            HorizontalDivider(color = AuraColors.outlineHairline)

            SettingsSectionHeader("Voice & Motion", icon = ChatIcons.Mic)
            CompactRow(
                label = "Speak responses",
                trailing = { Switch(checked = uiState.speakResponses, onCheckedChange = viewModel::setSpeakResponses) },
            )
            CompactRow(
                label = "Reduced motion",
                trailing = { Switch(checked = uiState.reducedMotion, onCheckedChange = viewModel::setReducedMotion) },
            )

            HorizontalDivider(color = AuraColors.outlineHairline)

            SettingsSectionHeader("Device capabilities", icon = Icons.Filled.Phone)
            CompactRow(
                label = "Set as default assistant",
                modifier = Modifier.clickable {
                    runCatching { context.startActivity(Intent(SystemSettings.ACTION_VOICE_INPUT_SETTINGS)) }
                },
            )
            // the ONLY gate for device_read_latest_sms/device_send_sms is the
            // OS runtime grant itself - no consent toggle, no explanation screen, no per-session
            // prompt (task brief, user law). Tapping while already granted is a no-op; the system
            // dialog handles "don't ask again" on its own.
            CompactRow(
                label = "SMS access",
                modifier = Modifier.clickable(enabled = !uiState.smsAccessGranted) {
                    smsPermissionLauncher.launch(arrayOf(Manifest.permission.READ_SMS, Manifest.permission.SEND_SMS))
                },
                trailing = {
                    Text(
                        text = if (uiState.smsAccessGranted) "Granted" else "Not granted",
                        style = AuraType.caption,
                    )
                },
            )
            // per-tool device-capability toggles, default ON (a tool is checked iff
            // its id is NOT in the disabled set). Toggling off removes the tool from BOTH advertisement
            // (DeviceToolCatalog) and execution (DeviceToolExecutor refuses it). Grouping/labels are
            // the canonical DeviceToolToggles.GROUPS (data/), never re-listed here.
            DeviceToolToggles.GROUPS.forEach { group ->
                DeviceToolGroupLabel(group.title)
                group.toggles.forEach { toggle ->
                    CompactRow(
                        label = toggle.label,
                        trailing = {
                            Switch(
                                checked = toggle.toolId !in uiState.disabledDeviceToolIds,
                                onCheckedChange = { enabled -> viewModel.setDeviceToolEnabled(toggle.toolId, enabled) },
                            )
                        },
                    )
                }
            }

            HorizontalDivider(color = AuraColors.outlineHairline)

            // the experimental Streamlit-widgets flag. This toggle only persists the
            // preference; the widget renderer consumes it at the advertise+render seam.
            SettingsSectionHeader("Widgets", icon = Icons.Filled.AddCircle)
            CompactRow(
                label = "Streamlit widgets",
                trailing = {
                    Switch(
                        checked = uiState.streamlitWidgetsEnabled,
                        onCheckedChange = viewModel::setStreamlitWidgetsEnabled,
                    )
                },
            )

            if (IS_DEBUG_BUILD) {
                HorizontalDivider(color = AuraColors.outlineHairline)
                SettingsSectionHeader("Debug", icon = Icons.Filled.Build)
                CompactRow(
                    label = "Use fake voice pipeline",
                    trailing = { Switch(checked = uiState.voiceUseFakes, onCheckedChange = viewModel::setVoiceUseFakes) },
                )
                // Zero-cost device testing: scripted sessions instead of the
                // real backend - every session/query/stream/models/projects/tools call gets a canned
                // response, no LLM spend. Same `-e mockBackend true` seed-extra pattern as
                // `seedBaseUrl`/`seedApiKey` lets automation flip this per-install (MainActivity).
                CompactRow(
                    label = "Use mock backend",
                    trailing = { Switch(checked = uiState.mockBackendEnabled, onCheckedChange = viewModel::setMockBackendEnabled) },
                )
                CompactRow(label = "Orb gallery", modifier = Modifier.clickable(onClick = onOpenOrbGallery))
                CompactRow(label = "Liveness gallery", modifier = Modifier.clickable(onClick = onOpenLivenessGallery))
                CompactRow(
                    label = "Test notice",
                    modifier = Modifier.clickable { noticeController.show("Test notice") },
                )
            }
            // Rows are contiguous (no per-row bottom padding, drawer-row style) - one closing
            // spacer so the last row doesn't sit flush against the screen/gesture-nav edge.
            Spacer(Modifier.height(AuraSpacing.screenGutter))
        }
    }

    if (projectPickerOpen) {
        ProjectPickerSheet(
            projects = uiState.projects,
            selectedKey = uiState.selectedProject,
            onSelect = { key ->
                viewModel.setSelectedProject(key)
                projectPickerOpen = false
            },
            onDismiss = { projectPickerOpen = false },
        )
    }

    // Both reuse the chat ModelPickerSheet verbatim (its own `models == null` degrade covers offline).
    if (appModelPickerOpen) {
        ModelPickerSheet(
            models = uiState.models,
            selectedModel = uiState.appDefaultModel.ifBlank { null },
            onSelect = { id ->
                viewModel.setAppDefaultModel(id)
                appModelPickerOpen = false
            },
            onDismiss = { appModelPickerOpen = false },
        )
    }
    if (overlayModelPickerOpen) {
        ModelPickerSheet(
            models = uiState.models,
            selectedModel = uiState.overlayDefaultModel.ifBlank { null },
            onSelect = { id ->
                viewModel.setOverlayDefaultModel(id)
                overlayModelPickerOpen = false
            },
            onDismiss = { overlayModelPickerOpen = false },
        )
    }
}

/**
 * Top-level settings section header (Connection/Identity/Defaults/Voice & Motion/Device
 * capabilities/Widgets/Debug) - a leading glyph plus the label, one row per section for
 * scan-ability (task brief). [icon] reuses [AuraSpacing.DrawerRow]'s existing icon geometry
 * (24dp / 12dp gap to label) rather than inventing a new size token, and is tinted
 * [AuraColors.textSecondary] to match [AuraType.sectionHeader]'s own color - the glyph reads as
 * one hierarchy step below body content, same tier as the label beside it, never louder. The
 * glyph is purely decorative (`contentDescription = null`); [text] alone is the accessible name,
 * and `.semantics { heading() }` marks the row as an a11y heading so TalkBack's heading-navigation
 * gesture can jump between sections.
 */
@Composable
private fun SettingsSectionHeader(text: String, icon: ImageVector) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .padding(
                top = AuraSpacing.DrawerRow.sectionHeaderTopPad,
                start = AuraSpacing.screenGutter,
                end = AuraSpacing.screenGutter,
            )
            .semantics { heading() },
    ) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            tint = AuraColors.textSecondary,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Text(text = text, style = AuraType.sectionHeader)
    }
}

/** A lighter sub-label above one device-tool cluster - `textSecondary` caption so
 * it reads one hierarchy step below [SettingsSectionHeader]'s "Device capabilities", never as a peer. */
@Composable
private fun DeviceToolGroupLabel(text: String) {
    Text(
        text = text,
        style = AuraType.caption,
        color = AuraColors.textSecondary,
        modifier = Modifier.padding(
            top = AuraSpacing.Composer.gapTight,
            start = AuraSpacing.screenGutter,
            end = AuraSpacing.screenGutter,
        ),
    )
}

/** "Your name" keeps the pre-existing immediate-apply-on-Done behavior (spec: only the connection
 * fields switch to explicit validate-and-save). */
@Composable
private fun NameField(displayName: String, onCommit: (String) -> Unit, modifier: Modifier = Modifier) {
    // Re-seeds only when the stored value changes (i.e. after a commit) - never mid-keystroke,
    // since nothing else writes this field between edits.
    var draft by remember(displayName) { mutableStateOf(displayName) }
    SlimTextField(
        label = "Your name",
        value = draft,
        onValueChange = { draft = it },
        keyboardType = KeyboardType.Text,
        imeAction = ImeAction.Done,
        onDone = { onCommit(draft) },
        modifier = modifier,
    )
}

/**
 * Base URL + API key: local draft state, no persistence on keystroke or IME action. The only path
 * to [com.mewbo.aura.data.settings.SettingsStore] is [onValidateAndSave] (probes first) or the
 * inline error row's "Save anyway" escape hatch ([onSaveAnyway]).
 */
@Composable
private fun ConnectionFields(
    uiState: SettingsUiState,
    onValidateAndSave: (baseUrl: String, apiKey: String) -> Unit,
    onSaveAnyway: (baseUrl: String, apiKey: String) -> Unit,
    onDismissError: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var draftBaseUrl by remember(uiState.baseUrl) { mutableStateOf(uiState.baseUrl) }
    var draftApiKey by remember(uiState.apiKey) { mutableStateOf(uiState.apiKey.orEmpty()) }

    Column(modifier = modifier.fillMaxWidth()) {
        SlimTextField(
            label = "Server base URL",
            value = draftBaseUrl,
            onValueChange = {
                draftBaseUrl = it
                onDismissError()
            },
            keyboardType = KeyboardType.Uri,
            imeAction = ImeAction.Next,
        )
        SlimTextField(
            label = "API key",
            value = draftApiKey,
            onValueChange = {
                draftApiKey = it
                onDismissError()
            },
            keyboardType = KeyboardType.Password,
            masked = true,
        )
        val error = uiState.connectionError
        if (error != null) {
            ErrorCard(
                reason = error,
                retryLabel = "Save anyway",
                onRetry = { onSaveAnyway(draftBaseUrl, draftApiKey) },
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.internalPadding),
            horizontalArrangement = Arrangement.End,
        ) {
            ValidateAndSaveButton(
                validating = uiState.validating,
                onClick = { onValidateAndSave(draftBaseUrl, draftApiKey) },
            )
        }
    }
}

@Composable
private fun ValidateAndSaveButton(validating: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Button(
        onClick = onClick,
        enabled = !validating,
        shape = AuraShape.radiusPill,
        colors = ButtonDefaults.buttonColors(
            containerColor = AuraColors.accentPrimary,
            contentColor = AuraColors.accentOnAccent,
            disabledContainerColor = AuraColors.accentMuted,
            disabledContentColor = AuraColors.accentOnAccent,
        ),
        modifier = modifier,
    ) {
        if (validating) {
            CircularProgressIndicator(
                color = AuraColors.accentOnAccent,
                strokeWidth = ValidateButtonSpinnerStroke,
                modifier = Modifier.size(ValidateButtonSpinnerSize),
            )
            Spacer(Modifier.width(AuraSpacing.Composer.gapTight))
        }
        Text(if (validating) "Validating…" else "Validate & save", style = AuraType.listItem)
    }
}

/** No token below [AuraSpacing.Composer.iconSize] (24dp) covers an inline button spinner - same
 * "flag in the task report, don't invent a token" gap [com.mewbo.aura.ui.common.ErrorCard]'s
 * `WarningGlyphSize` already documents. */
private val ValidateButtonSpinnerSize: Dp = 16.dp
private val ValidateButtonSpinnerStroke: Dp = 2.dp

/** Label caption + borderless [BasicTextField] - the drawer/search row idiom applied to an
 * editable field, replacing the boxed `OutlinedTextField` chrome this screen used before. */
@Composable
private fun SlimTextField(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    keyboardType: KeyboardType,
    modifier: Modifier = Modifier,
    imeAction: ImeAction = ImeAction.Next,
    masked: Boolean = false,
    onDone: (() -> Unit)? = null,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight),
    ) {
        Text(label, style = AuraType.caption)
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            textStyle = AuraType.listItem.copy(color = AuraColors.textPrimary),
            singleLine = true,
            cursorBrush = SolidColor(AuraColors.accentPrimary),
            visualTransformation = if (masked) PasswordVisualTransformation() else VisualTransformation.None,
            keyboardOptions = KeyboardOptions(keyboardType = keyboardType, imeAction = imeAction),
            // `null` falls through to Compose's own default per-action behavior (hide keyboard on
            // Done, advance focus on Next) - only the name field needs a real onDone.
            keyboardActions = onDone?.let { commit -> KeyboardActions(onDone = { commit() }) } ?: KeyboardActions.Default,
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

/** Fixed-height, contiguous, no-card-chrome row - the same shape as `ui/navigation`'s
 * `DrawerRow`, swapped in for the oversized `ListItem` rows this screen used before. */
@Composable
private fun CompactRow(
    label: String,
    modifier: Modifier = Modifier,
    trailing: (@Composable () -> Unit)? = null,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .padding(horizontal = AuraSpacing.screenGutter),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(text = label, style = AuraType.listItem, color = AuraColors.textPrimary, modifier = Modifier.weight(1f))
        trailing?.invoke()
    }
}
