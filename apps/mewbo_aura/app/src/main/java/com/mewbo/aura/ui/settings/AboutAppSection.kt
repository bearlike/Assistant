package com.mewbo.aura.ui.settings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mewbo.aura.data.update.AppUpdateState
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.common.LocalNoticeController
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import java.util.Locale

/**
 * What build is installed, and whether a newer one can be fetched from here.
 *
 * **Every state this section can be in leads somewhere, including the ones that lead nowhere.** A
 * build with no release source is not merely un-tappable by omission — its row carries no click and
 * no chevron at all, because a tap that does nothing is the one outcome this screen never ships
 * (`ui/settings/CLAUDE.md`). The same rule governs the install grant: when the device exposes no
 * screen for "Install unknown apps", the tap says so in a sentence rather than failing quietly.
 *
 * The action row's label, caption, trailing control and tap all come from ONE exhaustive `when`
 * over [AppUpdateState], so a new arm in the data layer is a compile error here rather than a row
 * that renders its previous state.
 */
@Composable
fun AboutAppSection(
    expansion: SectionExpansion,
    modifier: Modifier = Modifier,
    viewModel: AppUpdateViewModel = hiltViewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    // A delegated property cannot be smart-cast, and every branch below needs the concrete arm.
    val current = state
    val noticeController = LocalNoticeController.current

    // "Install unknown apps" is a SPECIAL grant: it is changed on a system screen and reports
    // nothing back, so there is no callback to observe and the only moment its answer can be
    // refreshed is the user returning to the app. Same shape the overlay-permission row uses.
    var canInstall by remember { mutableStateOf(viewModel.canInstallPackages()) }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) canInstall = viewModel.canInstallPackages()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    // The repository is a singleton, so its state outlives this screen: re-entering Settings must
    // not re-probe an answer already given, and must never interrupt a download in flight.
    LaunchedEffect(Unit) {
        if (state is AppUpdateState.NotChecked) viewModel.check()
    }

    val requestInstall = {
        // Asked rather than read from `canInstall`, which is only as fresh as the last resume.
        if (viewModel.canInstallPackages()) {
            viewModel.install()
        } else {
            canInstall = false
            if (!viewModel.openInstallPermissionScreen()) {
                noticeController.show(
                    "This device has no screen for the \"Install unknown apps\" permission, so the " +
                        "update cannot be installed from inside Aura.",
                )
            }
        }
    }

    val action = when (current) {
        AppUpdateState.NotChecked -> UpdateAction(
            label = "Check for updates",
            caption = "Asks the release feed for a newer build",
            onClick = viewModel::check,
            showChevron = true,
        )
        // No source to ask, so the row states that and offers no tap at all. A greyed-out chevron
        // would still read as "this will work once something changes"; nothing here will.
        AppUpdateState.Unsupported -> UpdateAction(
            label = "Check for updates",
            caption = "This build carries no update source",
        )
        AppUpdateState.Checking -> UpdateAction(label = "Checking…", busy = true)
        is AppUpdateState.UpToDate -> UpdateAction(
            label = "Check for updates",
            caption = "Nothing newer than ${current.installedVersion}",
            onClick = viewModel::check,
            showChevron = true,
        )
        is AppUpdateState.NoInstallableBuild -> UpdateAction(
            label = "Check for updates",
            caption = "${current.tagName} exists, but publishes no file for this device",
            onClick = viewModel::check,
            showChevron = true,
        )
        is AppUpdateState.CheckFailed -> UpdateAction(
            label = "Check for updates",
            onClick = viewModel::check,
            showChevron = true,
        )
        is AppUpdateState.Available -> UpdateAction(
            label = "Download ${current.update.versionLabel}",
            caption = describe(current.update.title, current.update.sizeBytes, current.update.prerelease),
            onClick = viewModel::download,
            showChevron = true,
        )
        is AppUpdateState.Downloading -> UpdateAction(
            label = "Downloading — ${(current.fraction * PercentScale).toInt()}%",
            caption = "Tap to cancel",
            onClick = viewModel::cancel,
            clickLabel = "Cancel",
        )
        is AppUpdateState.ReadyToInstall -> UpdateAction(
            label = "Install ${current.update.versionLabel}",
            caption = current.update.assetName,
            onClick = requestInstall,
            showChevron = true,
        )
        is AppUpdateState.Installing -> UpdateAction(label = "Installing…", busy = true)
        is AppUpdateState.Failed -> UpdateAction(
            label = "Try again",
            onClick = viewModel::download,
            showChevron = true,
        )
    }

    // The reason a failure gives is spelled out in the expanded card, never in the collapsed
    // header's badge — a glance has no room for a transport message, the same split the Connection
    // section makes.
    val failure = when (current) {
        is AppUpdateState.CheckFailed -> current.reason to viewModel::check
        is AppUpdateState.Failed -> current.reason to viewModel::download
        else -> null
    }

    val spinner: (@Composable () -> Unit)? = if (!action.busy) {
        null
    } else {
        {
            CircularProgressIndicator(
                color = AuraColors.accentPrimary,
                strokeWidth = InlineSpinnerStroke,
                modifier = Modifier.size(InlineSpinnerSize),
            )
        }
    }

    SettingsSection(
        id = "about",
        title = "About app",
        icon = Icons.Filled.Info,
        expansion = expansion,
        modifier = modifier,
        summary = updateBadge(current),
        caption = "Which build is running, and where a newer one comes from.",
    ) {
        SettingsRow(
            label = "Installed version",
            trailing = {
                Text(
                    text = viewModel.installedVersion,
                    style = AuraType.caption,
                    color = AuraColors.textSecondary,
                )
            },
        )
        SettingsRow(
            label = action.label,
            caption = action.caption,
            showChevron = action.showChevron,
            modifier = action.onClick?.let { click ->
                Modifier.clickable(onClickLabel = action.clickLabel, onClick = click)
            } ?: Modifier,
            trailing = spinner,
        )
        // Determinate from the first byte: the asset's size is declared by the release feed, so
        // there is never a stretch where the bar would have to guess.
        if (current is AppUpdateState.Downloading) {
            LinearProgressIndicator(
                progress = { current.fraction },
                color = AuraColors.accentPrimary,
                trackColor = AuraColors.accentMuted,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = AuraSpacing.Composer.internalPadding),
            )
        }
        // Only while an install is actually waiting: a grant row shown ahead of any download asks
        // the user for a permission nothing is yet using.
        if (current is AppUpdateState.ReadyToInstall && !canInstall) {
            SettingsRow(
                label = "Install unknown apps",
                caption = "Lets Mewbo install the update it downloaded",
                showChevron = true,
                modifier = Modifier.clickable {
                    if (!viewModel.openInstallPermissionScreen()) {
                        noticeController.show(
                            "This device has no screen for the \"Install unknown apps\" permission, so the " +
                                "update cannot be installed from inside Aura.",
                        )
                    }
                },
                trailing = { StatusBadgeText(grantBadge(canInstall)) },
            )
        }
        if (failure != null) {
            ErrorCard(
                reason = failure.first,
                onRetry = failure.second,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
    }
}

/**
 * One rendering of the action row, resolved from the update state before anything is composed.
 *
 * A [onClick] of `null` is the load-bearing case: the row is then not tappable at all, rather than
 * tappable with an empty body.
 */
private data class UpdateAction(
    val label: String,
    val caption: String? = null,
    val onClick: (() -> Unit)? = null,
    val showChevron: Boolean = false,
    val busy: Boolean = false,
    val clickLabel: String? = null,
)

/** The release's own title, its size, and whether it is a prerelease — whichever of the three the
 * release actually carries, in one caption-length phrase. */
private fun describe(title: String?, sizeBytes: Long, prerelease: Boolean): String? = listOfNotNull(
    title?.takeIf { it.isNotBlank() },
    humanSize(sizeBytes),
    "Prerelease".takeIf { prerelease },
).joinToString(" · ").takeIf { it.isNotBlank() }

/** A byte count as the person holding the device would read it. `null` when the feed declared no
 * size, because "0 MB" is a claim and an absent field is not. */
private fun humanSize(bytes: Long): String? = when {
    bytes <= 0 -> null
    bytes >= BytesPerMegabyte -> String.format(Locale.US, "%.1f MB", bytes.toDouble() / BytesPerMegabyte)
    else -> String.format(Locale.US, "%d KB", bytes / BytesPerKilobyte)
}

private const val BytesPerKilobyte = 1024L
private const val BytesPerMegabyte = BytesPerKilobyte * 1024L
private const val PercentScale = 100f

/** No token below [AuraSpacing.Composer.iconSize] (24dp) covers an inline spinner — the same gap
 * `SettingsScreen`'s `ValidateButtonSpinnerSize` already documents, and a theme token invented for
 * one row would be a token nothing else can consume. */
private val InlineSpinnerSize: Dp = 16.dp
private val InlineSpinnerStroke: Dp = 2.dp
