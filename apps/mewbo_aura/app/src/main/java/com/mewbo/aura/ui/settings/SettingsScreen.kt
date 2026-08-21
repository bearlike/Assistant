package com.mewbo.aura.ui.settings

import android.app.Activity
import android.Manifest
import android.content.Intent
import android.os.Build
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
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.AddCircle
import androidx.compose.material.icons.filled.Alarm
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Lightbulb
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material.icons.filled.Security
import androidx.compose.material.icons.filled.Sms
import androidx.compose.material.icons.filled.Star
import androidx.compose.material.icons.filled.TouchApp
import androidx.compose.material.icons.filled.Tune
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
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mewbo.aura.IS_DEBUG_BUILD
import com.mewbo.aura.data.device.DeviceToolCatalog
import com.mewbo.aura.data.device.DeviceToolToggles
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.model.SpeechDirection
import com.mewbo.aura.debugtools.DebugTool
import com.mewbo.aura.debugtools.DebugToolsEntryPoint
import com.mewbo.aura.ui.chat.ChatIcons
import com.mewbo.aura.ui.chat.ModelPickerSheet
import com.mewbo.aura.ui.common.AuraListBottomSheet
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.common.LocalDeviceShape
import com.mewbo.aura.ui.common.LocalNoticeController
import com.mewbo.aura.ui.common.auraFocusRing
import com.mewbo.aura.ui.common.dpadFocusEscape
import com.mewbo.aura.ui.common.imeOnConfirmOnly
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.voice.SpeechVolumeBoost
import dagger.hilt.android.EntryPointAccessors

/**
 * Settings, as a stack of collapsible sections rather than one long scroll of bare controls.
 *
 * Two things the old screen could not tell you, and both are the point of this one:
 *
 * **What a control governs.** Every row with a non-obvious reach carries a purpose caption under
 * its label — a phrase, never a sentence. "Default model" named a setting and left the user to
 * discover by trying it that it reaches new in-app sessions and not the overlay, which has its own.
 * The captions are always visible rather than hidden behind a tooltip, because a control whose
 * scope is invisible is not helped by an explanation that is also invisible.
 *
 * **Whether it is on.** Permissions and the connection report state as glyph plus word plus tint
 * ([StatusBadgeText]) — never a tint alone. A section reports the same state while COLLAPSED, so
 * folding the screen down hides controls without hiding facts.
 *
 * **Anything not reliably readable renders as unknown.** The states this screen can and cannot
 * read are enumerated in `ui/settings/CLAUDE.md`; a green row over a state nobody actually queried
 * sends the user hunting for a bug in Mewbo instead of a grant in Android.
 *
 * The one grouping decision that is not cosmetic: **permissions and device tools are separate
 * sections.** Android grants the first set and Mewbo can only ask; the user owns the second set
 * outright. They fail differently, so they are answered differently, and reading as one list was
 * what made "Screen control: Ready" look like a switch rather than a grant.
 *
 * Connection fields remain the one exception to immediate-apply: they hold local draft state and
 * reach [com.mewbo.aura.data.settings.SettingsStore] only through "Validate & save", which probes
 * them live first ([SettingsViewModel.validateAndSave]).
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
    // Not on `SettingsViewModel`: a launcher is not state, and the ViewModel would have to hold an
    // Activity context to use it. `EntryPointAccessors` is this codebase's sanctioned way to reach
    // an existing singleton binding from a composable (see `voice/AssistEntryPoint`), and the
    // binding it resolves is a no-op in release.
    val debugTools = remember(context) {
        EntryPointAccessors.fromApplication(context.applicationContext, DebugToolsEntryPoint::class.java).debugTools()
    }
    val noticeController = LocalNoticeController.current
    val expansion = rememberSaveable(saver = SectionExpansion.Saver) { SectionExpansion() }
    var projectPickerOpen by remember { mutableStateOf(false) }
    // two independent model-default pickers (app vs assist overlay), each reusing
    // the chat ModelPickerSheet + lazy catalog load, mirroring the project picker's shape above.
    var appModelPickerOpen by remember { mutableStateOf(false) }
    var overlayModelPickerOpen by remember { mutableStateOf(false) }
    // The two speech-engine pickers share ONE catalog (both directions arrive in a single
    // response), so either opening is enough to fetch it.
    var sttPickerOpen by remember { mutableStateOf(false) }
    var ttsPickerOpen by remember { mutableStateOf(false) }
    // A picker rather than a slider: a Compose `Slider` is draggable and its D-pad behaviour is not
    // something this app has measured on a remote, while a list of rows is the one selection
    // vocabulary the whole screen already traverses correctly. Stepped levels are also what a
    // control operated from across a room actually needs.
    var boostPickerOpen by remember { mutableStateOf(false) }

    LaunchedEffect(projectPickerOpen) {
        if (projectPickerOpen) viewModel.loadProjectsIfNeeded { noticeController.show(it) }
    }
    LaunchedEffect(appModelPickerOpen, overlayModelPickerOpen) {
        if (appModelPickerOpen || overlayModelPickerOpen) viewModel.loadModelsIfNeeded { noticeController.show(it) }
    }
    LaunchedEffect(sttPickerOpen, ttsPickerOpen) {
        if (sttPickerOpen || ttsPickerOpen) viewModel.loadSpeechEnginesIfNeeded { noticeController.show(it) }
    }
    // Stored credentials say only that someone typed something once. One probe per screen turns
    // that into an answer the collapsed Connection card can report.
    LaunchedEffect(Unit) { viewModel.checkStoredConnection() }
    // D-pad entry lands on the first section header rather than nowhere. `runCatching` because the
    // header node may not be attached to the composition yet on the very first frame.
    val firstSectionFocusRequester = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { firstSectionFocusRequester.requestFocus() } }
    // Live OS reads, not persisted preferences - re-read on every RESUME rather than once per
    // composition. Granting a permission, setting the assistant, and starting Shizuku all happen in
    // another app, so the moment that matters is the user coming back; a one-shot effect would
    // leave every row showing the state from before they left. "Display over other apps" has no
    // dialog and therefore no result callback at all, so for that row this is the ONLY refresh.
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                viewModel.refreshSystemPermissions()
                viewModel.refreshDeviceControlStatus()
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    val smsPermissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        viewModel.refreshSystemPermissions()
    }
    val notificationPermissionLauncher =
        rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
            viewModel.refreshSystemPermissions()
        }
    val asked by viewModel.askedPermissions.collectAsStateWithLifecycle()
    // The Activity is what answers "will the dialog still appear" — a permission
    // denied permanently makes the request a silent no-op, and only
    // shouldShowRequestPermissionRationale (an Activity API) can tell.
    val permissions = remember(context) { PermissionRequest(context, context as? Activity) }

    val shizuku = shizukuBadge(uiState.deviceControlStatus)
    val permissionTones = systemPermissionTones(uiState)

    Scaffold(
        modifier = modifier,
        containerColor = AuraColors.surfaceCanvas,
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onBack, modifier = Modifier.auraFocusRing(shape = AuraShape.radiusPill)) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back", tint = AuraColors.iconPrimary)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = AuraColors.surfaceCanvas),
            )
        },
    ) { padding ->
        Column(
            verticalArrangement = Arrangement.spacedBy(AuraSpacing.Settings.cardGap),
            modifier = Modifier
                .fillMaxWidth()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = AuraSpacing.Composer.horizontalMargin),
        ) {
            SettingsSection(
                id = "connection",
                title = "Connection and identity",
                icon = Icons.Filled.Lock,
                expansion = expansion,
                summary = uiState.connectionStatus.badge,
                caption = "Where Mewbo runs and how this device signs in.",
                headerFocusRequester = firstSectionFocusRequester,
            ) {
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
                NameField(displayName = uiState.displayName, onCommit = viewModel::setDisplayName)
            }

            SettingsSection(
                id = "defaults",
                title = "Defaults",
                icon = Icons.Filled.Star,
                expansion = expansion,
                summary = StatusBadge(
                    resolveProjectDisplayName(uiState.selectedProject, uiState.projects),
                    StatusTone.Value,
                ),
                caption = "What a new session inherits when you do not choose otherwise.",
            ) {
                SettingsRow(
                    label = "Default project",
                    caption = "New sessions start in this project",
                    showChevron = true,
                    modifier = Modifier.clickable { projectPickerOpen = true },
                    trailing = {
                        Text(
                            text = resolveProjectDisplayName(uiState.selectedProject, uiState.projects),
                            style = AuraType.caption,
                            color = AuraColors.textSecondary,
                        )
                    },
                )
                SettingsRow(
                    label = "Default model",
                    caption = "New sessions you start in the app",
                    showChevron = true,
                    modifier = Modifier.clickable { appModelPickerOpen = true },
                    trailing = {
                        Text(
                            text = resolveModelDisplayName(uiState.appDefaultModel, uiState.models),
                            style = AuraType.caption,
                            color = AuraColors.textSecondary,
                        )
                    },
                )
                SettingsRow(
                    label = "Assistant overlay model",
                    caption = "Sessions started from the overlay",
                    showChevron = true,
                    modifier = Modifier.clickable { overlayModelPickerOpen = true },
                    trailing = {
                        Text(
                            text = resolveModelDisplayName(uiState.overlayDefaultModel, uiState.models),
                            style = AuraType.caption,
                            color = AuraColors.textSecondary,
                        )
                    },
                )
            }

            SettingsSection(
                id = "voice",
                title = "Voice & Motion",
                icon = ChatIcons.Mic,
                expansion = expansion,
            ) {
                SettingsRow(
                    label = "Speak responses",
                    caption = "Reads replies aloud as they arrive",
                    modifier = Modifier.clickable {
                        viewModel.setSpeakResponses(!uiState.speakResponses)
                    },
                    trailing = {
                        Switch(checked = uiState.speakResponses, onCheckedChange = null)
                    },
                )
                // Two engine rows, structurally identical to the "Defaults" model rows above: a
                // chevron, a tap that opens a picker, and the resolved name as trailing text. The
                // name carries its own cloud mark for a server engine, so the collapsed row states
                // where the audio goes without needing a second indicator.
                SettingsRow(
                    label = "Speech to text",
                    caption = "Dictation and the assistant overlay",
                    showChevron = true,
                    modifier = Modifier.clickable { sttPickerOpen = true },
                    trailing = {
                        Text(
                            text = resolveSpeechEngineName(
                                uiState.speechToTextEngine,
                                SpeechDirection.SpeechToText,
                                uiState.speechEngines,
                            ),
                            style = AuraType.caption,
                            color = AuraColors.textSecondary,
                        )
                    },
                )
                SettingsRow(
                    label = "Text to speech",
                    caption = "Reading replies aloud",
                    showChevron = true,
                    modifier = Modifier.clickable { ttsPickerOpen = true },
                    trailing = {
                        Text(
                            text = resolveSpeechEngineName(
                                uiState.textToSpeechEngine,
                                SpeechDirection.TextToSpeech,
                                uiState.speechEngines,
                            ),
                            style = AuraType.caption,
                            color = AuraColors.textSecondary,
                        )
                    },
                )
                // Adjacent to "Text to speech" because it governs the same output, whichever engine
                // that row selected. The trailing slot shows the LEVEL and makes no state claim
                // (StatusTone.Value's job); a badge replaces it only where an attach was actually
                // attempted and refused.
                SettingsRow(
                    label = "Volume boost",
                    caption = "Makes spoken replies louder than the device's own maximum",
                    showChevron = true,
                    modifier = Modifier.clickable { boostPickerOpen = true },
                    trailing = {
                        if (uiState.speechVolumeBoostRefused) {
                            StatusBadgeText(StatusBadge("Not supported here", StatusTone.Problem))
                        } else {
                            Text(
                                text = resolveVolumeBoostLabel(uiState.speechVolumeBoostDecibels),
                                style = AuraType.caption,
                                color = AuraColors.textSecondary,
                            )
                        }
                    },
                )
                SettingsRow(
                    label = "Reduced motion",
                    caption = "Stills animation everywhere in the app",
                    modifier = Modifier.clickable {
                        viewModel.setReducedMotion(!uiState.reducedMotion)
                    },
                    trailing = {
                        Switch(checked = uiState.reducedMotion, onCheckedChange = null)
                    },
                )
            }

            // Android owns everything in this section. Mewbo can ask and can point you at the
            // screen that grants it; it can never grant anything itself, which is exactly why
            // these rows report a STATE rather than offering a switch.
            SettingsSection(
                id = "permissions",
                title = "System permissions",
                icon = Icons.Filled.Security,
                expansion = expansion,
                summary = permissionSummary(permissionTones),
                caption = "Android grants these. Mewbo can only ask, and tapping a row asks.",
            ) {
                // The row states the role and nothing more. Whether Mewbo HOLDS it is a
                // RoleManager read; whether the system will let you change it here is not
                // knowable, so the tap opens the picker and lets the system answer.
                //
                // Absent entirely on a television rather than disabled — a greyed row would still
                // claim the capability is coming (why: app CLAUDE.md § "TV-shape facts").
                // `systemPermissionTones` MUST drop the same row from the header's count; the two
                // are one fact in two files and are pinned together by TelevisionSurfacesTest.
                if (!uiState.isTelevision) {
                    SettingsRow(
                        label = "Default assistant",
                        caption = "Opens Mewbo from the system assistant gesture",
                        showChevron = true,
                        modifier = Modifier.clickable {
                            runCatching { context.startActivity(Intent(SystemSettings.ACTION_VOICE_INPUT_SETTINGS)) }
                        },
                        trailing = { StatusBadgeText(uiState.assistantRole.badge) },
                    )
                }
                // POST_NOTIFICATIONS was previously requested only at first query send,
                // so a single denial left no route back to it from inside the app.
                //
                // Below API 33 there is no runtime permission dialog for this at all — asking
                // for one is a silent no-op, not a prompt — so the tap goes straight to the
                // app's own settings page (Notifications is one tap from there), the same
                // fall-through already used once this permission is permanently denied above 33.
                SettingsRow(
                    label = "Notifications",
                    caption = "Posts a notice when a run finishes while you are away",
                    showChevron = !uiState.notificationsGranted,
                    modifier = Modifier.clickable(enabled = !uiState.notificationsGranted) {
                        val permission = Manifest.permission.POST_NOTIFICATIONS
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                            permissions.canPrompt(permission in asked, permission)
                        ) {
                            viewModel.markPermissionAsked(permission)
                            notificationPermissionLauncher.launch(permission)
                        } else {
                            permissions.openAppSettings()
                        }
                    },
                    trailing = { StatusBadgeText(grantBadge(uiState.notificationsGranted)) },
                )
                // the ONLY gate for device_read_latest_sms/device_send_sms is the
                // OS runtime grant itself - no consent toggle, no explanation screen, no per-session
                // prompt (task brief, user law). Tapping while already granted is a no-op; the system
                // dialog handles "don't ask again" on its own.
                // Tappable even once denied. A row wired straight to the launcher does
                // NOTHING on a permanently-denied permission — no dialog, no message —
                // which reads as a broken button; the fall-through opens the app's own
                // settings page, where the grant is still reachable.
                SettingsRow(
                    label = "SMS access",
                    caption = "Lets Mewbo read and send text messages",
                    showChevron = !uiState.smsAccessGranted,
                    modifier = Modifier.clickable(enabled = !uiState.smsAccessGranted) {
                        val smsPermissions =
                            arrayOf(Manifest.permission.READ_SMS, Manifest.permission.SEND_SMS)
                        if (permissions.canPrompt(smsPermissions.any { it in asked }, *smsPermissions)) {
                            viewModel.markPermissionAsked(*smsPermissions)
                            smsPermissionLauncher.launch(smsPermissions)
                        } else {
                            permissions.openAppSettings()
                        }
                    },
                    trailing = { StatusBadgeText(grantBadge(uiState.smsAccessGranted)) },
                )
                // Named for the thing being granted. "Screen control: Ready" named a capability and
                // hid its cause, so its "Off" covered three states needing three different actions.
                // The service does not survive a reboot on a non-rooted device, so "not running" is
                // a normal recurring state rather than a fault. EVERY not-ready state leads
                // somewhere: the tap asks Shizuku while its dialog can still appear, and otherwise
                // opens Shizuku itself, which is where the service is restarted after a reboot and
                // where a permanent denial can be undone.
                SettingsRow(
                    label = "Shizuku access",
                    caption = "Lets Mewbo see the screen and drive it",
                    showChevron = uiState.deviceControlStatus != DeviceControlStatus.Ready,
                    modifier = Modifier.clickable(
                        enabled = uiState.deviceControlStatus != DeviceControlStatus.Ready,
                    ) {
                        if (uiState.deviceControlStatus == DeviceControlStatus.PermissionDenied) {
                            if (!viewModel.requestDeviceControlPermission()) permissions.openShizuku()
                        } else {
                            permissions.openShizuku()
                        }
                    },
                    trailing = { StatusBadgeText(shizuku) },
                )
                // Sits under Shizuku because the two are the halves of one story: Shizuku is what
                // lets an agent drive the phone, and this is what lets you SEE that it is. Denied,
                // device control still works in full — it just runs with no glow, no narration and
                // no Stop pill, which is the invisible-agent case the overlay exists to remove. So
                // the caption names the consequence rather than the mechanism.
                //
                // Named "Display over other apps" after the system toggle itself, so the screen the
                // tap opens reads as the row the user just pressed. There is no dialog for a
                // special permission; the deep link IS the request, and the grant lands with no
                // callback, which is why the row's truth comes from the resume re-read above.
                //
                // The TAP is provisioned per device shape, because the two shapes do not have the
                // same routes available. A handheld has the system screen and the deep link IS the
                // request. A television does not expose that screen at all — neither
                // ACTION_MANAGE_OVERLAY_PERMISSION nor the app-details page carries the toggle — so
                // the same tap would open nothing and the row would read as broken. There it writes
                // the app-op through Shizuku instead, which reports its own outcome (including
                // every refusal) as a sentence, since a special permission has no result callback.
                val hasSystemOverlayScreen = LocalDeviceShape.current.hasOverlayPermissionScreen
                SettingsRow(
                    label = "Display over other apps",
                    caption = if (hasSystemOverlayScreen) {
                        "Shows on-screen when an agent is driving your phone"
                    } else {
                        "Shows on-screen when an agent is driving this device. Granted through Shizuku — this device has no system screen for it."
                    },
                    showChevron = !uiState.overlayPermissionGranted,
                    modifier = Modifier.clickable(enabled = !uiState.overlayPermissionGranted) {
                        if (hasSystemOverlayScreen) {
                            permissions.openOverlaySettings()
                        } else {
                            viewModel.grantOverlayPermissionViaShizuku { outcome -> noticeController.show(outcome.message) }
                        }
                    },
                    trailing = { StatusBadgeText(grantBadge(uiState.overlayPermissionGranted)) },
                )
            }

            // per-tool device-capability toggles, default ON (a tool is checked iff
            // its id is NOT in the disabled set). Toggling off removes the tool from BOTH advertisement
            // (DeviceToolCatalog) and execution (DeviceToolExecutor refuses it). Grouping/labels are
            // the canonical DeviceToolToggles.GROUPS (data/), never re-listed here.
            val allToggles = remember { DeviceToolToggles.GROUPS.flatMap { it.toggles } }
            SettingsSection(
                id = "tools",
                title = "Device tools",
                icon = Icons.Filled.Tune,
                expansion = expansion,
                summary = toolSummary(
                    enabled = allToggles.count { it.toolId !in uiState.disabledDeviceToolIds },
                    total = allToggles.size,
                ),
                caption = "What Mewbo may reach for on this device. You own these outright.",
            ) {
                DeviceToolToggles.GROUPS.forEach { group ->
                    SettingsSubheader(group.title, icon = DeviceToolGroupGlyphs.forTitle(group.title))
                    group.toggles.forEach { toggle ->
                        // A screen-control switch is DISABLED until the capability is
                        // actually available. An enabled-looking switch reads as "this
                        // works", so leaving it live while Shizuku is down makes the
                        // screen claim a capability the session does not have — the
                        // user then reasonably reports the agent as broken rather than
                        // the grant as missing. The stored INTENT is untouched; only
                        // the affordance waits for the capability to be real.
                        val needsDeviceControl = toggle.toolId in DeviceToolCatalog.CONTROL_TOOL_IDS
                        val usable = !needsDeviceControl || uiState.deviceControlStatus.isReady
                        SettingsRow(
                            label = toggle.label,
                            caption = if (usable) null else "Waiting on Shizuku access",
                            // A disabled switch must not become row-activatable — that would let a
                            // remote toggle a capability the touch UI itself refuses to offer.
                            modifier = if (usable) {
                                Modifier.clickable {
                                    viewModel.setDeviceToolEnabled(
                                        toggle.toolId,
                                        toggle.toolId in uiState.disabledDeviceToolIds,
                                    )
                                }
                            } else {
                                Modifier
                            },
                            trailing = {
                                Switch(
                                    checked = toggle.toolId !in uiState.disabledDeviceToolIds && usable,
                                    enabled = usable,
                                    onCheckedChange = null,
                                )
                            },
                        )
                    }
                }
            }

            // the experimental Streamlit-widgets flag. This toggle only persists the
            // preference; the widget renderer consumes it at the advertise+render seam.
            SettingsSection(
                id = "widgets",
                title = "Widgets",
                icon = Icons.Filled.AddCircle,
                expansion = expansion,
            ) {
                SettingsRow(
                    label = "Streamlit widgets",
                    caption = "Renders interactive widgets inside a reply",
                    modifier = Modifier.clickable {
                        viewModel.setStreamlitWidgetsEnabled(!uiState.streamlitWidgetsEnabled)
                    },
                    trailing = {
                        Switch(checked = uiState.streamlitWidgetsEnabled, onCheckedChange = null)
                    },
                )
            }

            AboutAppSection(expansion = expansion)

            if (IS_DEBUG_BUILD) {
                SettingsSection(
                    id = "debug",
                    title = "Debug",
                    icon = Icons.Filled.Build,
                    expansion = expansion,
                ) {
                    SettingsRow(
                        label = "Use fake voice pipeline",
                        caption = "Scripted speech in place of the real recognizer",
                        modifier = Modifier.clickable {
                            viewModel.setVoiceUseFakes(!uiState.voiceUseFakes)
                        },
                        trailing = {
                            Switch(checked = uiState.voiceUseFakes, onCheckedChange = null)
                        },
                    )
                    // Zero-cost device testing: scripted sessions instead of the
                    // real backend - every session/query/stream/models/projects/tools call gets a canned
                    // response, no LLM spend. Same `-e mockBackend true` seed-extra pattern as
                    // `seedBaseUrl`/`seedApiKey` lets automation flip this per-install (MainActivity).
                    SettingsRow(
                        label = "Use mock backend",
                        caption = "Canned replies, no server and no model spend",
                        modifier = Modifier.clickable {
                            viewModel.setMockBackendEnabled(!uiState.mockBackendEnabled)
                        },
                        trailing = {
                            Switch(checked = uiState.mockBackendEnabled, onCheckedChange = null)
                        },
                    )
                    SettingsRow(label = "Orb gallery", showChevron = true, modifier = Modifier.clickable(onClick = onOpenOrbGallery))
                    SettingsRow(label = "Liveness gallery", showChevron = true, modifier = Modifier.clickable(onClick = onOpenLivenessGallery))
                    // Debug tools hosted by their OWN Activity rather than a nav route, so they are
                    // reached through the `DebugTools` seam instead of a route constant — a `main/`
                    // caller must never name a `src/debug` class (see that interface's KDoc). The
                    // availability check is what keeps a variant without the tool from rendering a
                    // row that does nothing.
                    DebugTool.entries.filter(debugTools::isAvailable).forEach { tool ->
                        SettingsRow(
                            label = tool.label,
                            caption = tool.caption,
                            showChevron = true,
                            modifier = Modifier.clickable { debugTools.launch(context, tool) },
                        )
                    }
                    // The escape hatch. Every teardown path on the device-control overlay is
                    // cooperative — the windows are raised and lowered by the grant collector alone
                    // — so the state this escapes is the one where that did not cooperate and
                    // nothing is left that will take the surface away. It reports what actually
                    // happened rather than a flat success: on a shape where the overlay's own Stop
                    // is unreachable, "Overlay cleared" for a screen that had none would be the
                    // guess this surface's own rule forbids.
                    SettingsRow(
                        label = "Force-clear device overlay",
                        caption = "Removes a stuck control overlay and ends the grant",
                        showChevron = true,
                        modifier = Modifier.clickable {
                            noticeController.show(
                                if (viewModel.forceClearDeviceOverlay()) {
                                    "Overlay cleared and device control ended"
                                } else {
                                    "Nothing to clear — no overlay was showing"
                                },
                            )
                        },
                    )
                    SettingsRow(
                        label = "Test notice",
                        showChevron = true,
                        modifier = Modifier.clickable { noticeController.show("Test notice") },
                    )
                }
            }
            // One closing spacer so the last card doesn't sit flush against the screen/gesture-nav edge.
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

    // Its own sheet rather than ModelPickerSheet: speech engines are a different namespace with no
    // popular/more partitioning, and the sheet CONTAINER is shared either way.
    if (sttPickerOpen) {
        SpeechEnginePickerSheet(
            catalog = uiState.speechEngines,
            direction = SpeechDirection.SpeechToText,
            selectedId = uiState.speechToTextEngine,
            onSelect = { id ->
                viewModel.setSpeechToTextEngine(id)
                sttPickerOpen = false
            },
            onDismiss = { sttPickerOpen = false },
        )
    }
    if (ttsPickerOpen) {
        SpeechEnginePickerSheet(
            catalog = uiState.speechEngines,
            direction = SpeechDirection.TextToSpeech,
            selectedId = uiState.textToSpeechEngine,
            onSelect = { id ->
                viewModel.setTextToSpeechEngine(id)
                ttsPickerOpen = false
            },
            onDismiss = { ttsPickerOpen = false },
        )
    }
    if (boostPickerOpen) {
        VolumeBoostPickerSheet(
            selectedDecibels = uiState.speechVolumeBoostDecibels,
            onSelect = { decibels ->
                viewModel.setSpeechVolumeBoostDecibels(decibels)
                boostPickerOpen = false
            },
            onDismiss = { boostPickerOpen = false },
        )
    }
}

/**
 * Picks how much to amplify spoken replies, from the stepped levels
 * [SpeechVolumeBoost.LEVELS_DECIBELS] offers.
 *
 * Rows rather than a slider, and the CONTAINER is the shared [AuraListBottomSheet] — nothing about
 * bounding, scrolling or insets is re-derived here (`ui/common/CLAUDE.md`: the container is never
 * the thing you fork). Each row leads with [auraFocusRing] BEFORE its click, per DESIGN.md §7.27; a
 * ring appended after a click modifier compiles, focuses correctly and draws nothing.
 *
 * The header states the one thing a user cannot discover by trying it: on the on-device engine the
 * boost depends on that engine honouring an audio session id, and some do not.
 */
@Composable
private fun VolumeBoostPickerSheet(
    selectedDecibels: Int,
    onSelect: (Int) -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    AuraListBottomSheet(
        onDismiss = onDismiss,
        modifier = modifier,
        header = {
            Text(
                text = "Adds loudness on top of the device's own volume. Some on-device speech " +
                    "engines ignore it.",
                style = AuraType.caption,
                color = AuraColors.textTertiary,
                modifier = Modifier.padding(horizontal = AuraSpacing.screenGutter),
            )
        },
    ) {
        items(SpeechVolumeBoost.LEVELS_DECIBELS, key = { it }) { decibels ->
            val label = resolveVolumeBoostLabel(decibels)
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .auraFocusRing()
                    .fillMaxWidth()
                    .height(AuraSpacing.DrawerRow.height)
                    .clickable { onSelect(decibels) }
                    .padding(horizontal = AuraSpacing.screenGutter),
            ) {
                Text(
                    text = label,
                    style = AuraType.listItem,
                    color = AuraColors.textPrimary,
                    modifier = Modifier.weight(1f),
                )
                if (decibels == selectedDecibels) {
                    Icon(
                        imageVector = Icons.Filled.Check,
                        contentDescription = "Selected",
                        tint = AuraColors.accentPrimary,
                        modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
                    )
                }
            }
        }
    }
}

/**
 * Leading glyph for one `DeviceToolToggles` cluster.
 *
 * Matched on the canonical group title, which lives in `data/` — an unrecognized title falls
 * through to the generic glyph rather than failing to render, the same forward-compatible posture
 * `ActivityToolGlyphs` takes for an unknown tool id. All Filled weight, per the one-weight-per-
 * surface rule this screen shares with the drawer.
 */
private object DeviceToolGroupGlyphs {
    fun forTitle(title: String): ImageVector = when (title) {
        "Time & battery" -> Icons.Filled.Schedule
        "Alarms & timers" -> Icons.Filled.Alarm
        "Attention" -> Icons.Filled.Lightbulb
        "Messaging" -> Icons.Filled.Sms
        "Screen control" -> Icons.Filled.TouchApp
        else -> Icons.Filled.Build
    }
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
        caption = "How Mewbo addresses you",
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
        val probeFailure = (uiState.connectionStatus as? ConnectionStatus.Failed)?.reason
        when {
            // An explicit "Validate & save" that failed. There IS a draft to keep, so the card
            // offers the escape hatch.
            error != null -> ErrorCard(
                reason = error,
                retryLabel = "Save anyway",
                onRetry = { onSaveAnyway(draftBaseUrl, draftApiKey) },
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
            // The screen's own probe of the ALREADY-stored credentials failed. Nothing to save, so
            // no escape hatch — this is the header's "Not reachable" spelled out in full.
            probeFailure != null -> ErrorCard(
                reason = probeFailure,
                onRetry = null,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(
                    horizontal = AuraSpacing.Composer.internalPadding,
                    vertical = AuraSpacing.Composer.gapTight,
                ),
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
        modifier = modifier.auraFocusRing(shape = AuraShape.radiusPill),
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

/**
 * Label caption + borderless [BasicTextField] - the drawer/search row idiom applied to an editable
 * field.
 *
 * Vertical padding is a full [AuraSpacing.Composer.internalPadding] rather than the tight gap it
 * carried before: the name field's own subtext sat hard against the section boundary underneath it,
 * so the field read as crowded into the border rather than as a control with its own room.
 */
@Composable
private fun SlimTextField(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    keyboardType: KeyboardType,
    modifier: Modifier = Modifier,
    caption: String? = null,
    imeAction: ImeAction = ImeAction.Next,
    masked: Boolean = false,
    onDone: (() -> Unit)? = null,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(
                horizontal = AuraSpacing.Composer.internalPadding,
                vertical = AuraSpacing.Composer.internalPadding,
            ),
    ) {
        Text(label, style = AuraType.caption, color = AuraColors.textSecondary)
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
            // Settings is a long column of fields; without the escape a remote stops at the first
            // one, and without the gate every pass THROUGH one raises the IME over the screen.
            // Plain String state, so the coarser no-selection table applies.
            modifier = Modifier
                .fillMaxWidth()
                .dpadFocusEscape()
                .imeOnConfirmOnly()
                .padding(top = AuraSpacing.Settings.captionGap),
        )
        if (caption != null) {
            Text(
                text = caption,
                style = AuraType.caption,
                color = AuraColors.textTertiary,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
    }
}
