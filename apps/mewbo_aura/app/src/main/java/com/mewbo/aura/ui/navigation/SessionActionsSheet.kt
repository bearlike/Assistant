package com.mewbo.aura.ui.navigation

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.outlined.Edit
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.SessionSummary
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType
import com.mewbo.aura.ui.theme.VectorGlyphFill

private sealed interface ActionsPane {
    data object Root : ActionsPane
    data object Rename : ActionsPane
}

/**
 * Long-press session-row actions (drawer's Recents rail): Rename / Archive, reference-app-like
 * anatomy over house tokens (same `surfaceInput`/`radiusBubble`/56dp-row shape as
 * `ComposerOptionsSheet`/`ModelPickerSheet`). [onRename]/[onArchive] delegate to
 * [com.mewbo.aura.ui.sessions.SessionsViewModel]'s action methods — this composable owns no
 * repository access, only the in-sheet pane/busy/error UI state; each callback's own
 * `onResult: (Boolean) -> Unit` is invoked once the caller's coroutine settles. On success this
 * sheet dismisses itself ([onDismiss]); on failure it stays open with a one-line inline error
 * caption and the attempted action is retryable. Long-press already fired the row's one haptic
 * ([AuraDrawerContent]'s `combinedClickable`) — nothing in here fires a second.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SessionActionsSheet(
    session: SessionSummary,
    onDismiss: () -> Unit,
    onRename: (title: String, onResult: (Boolean) -> Unit) -> Unit,
    onArchive: (onResult: (Boolean) -> Unit) -> Unit,
    modifier: Modifier = Modifier,
) {
    var pane by remember { mutableStateOf<ActionsPane>(ActionsPane.Root) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(),
        containerColor = AuraColors.surfaceInput,
        shape = SheetShape,
        modifier = modifier,
    ) {
        when (pane) {
            ActionsPane.Root -> RootActionsPane(
                session = session,
                busy = busy,
                error = error,
                onRenameTap = { error = null; pane = ActionsPane.Rename },
                onArchiveTap = {
                    error = null
                    busy = true
                    onArchive { success ->
                        busy = false
                        if (success) onDismiss() else error = "Couldn't archive session"
                    }
                },
            )
            ActionsPane.Rename -> RenamePane(
                initialTitle = session.title.orEmpty(),
                busy = busy,
                error = error,
                onBack = { error = null; pane = ActionsPane.Root },
                onCommit = { title ->
                    error = null
                    busy = true
                    onRename(title) { success ->
                        busy = false
                        if (success) onDismiss() else error = "Couldn't rename session"
                    }
                },
            )
        }
    }
}

@Composable
private fun RootActionsPane(
    session: SessionSummary,
    busy: Boolean,
    error: String?,
    onRenameTap: () -> Unit,
    onArchiveTap: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxWidth().padding(bottom = AuraSpacing.Composer.internalPadding)) {
        SessionHeader(session = session)
        HorizontalDivider(color = AuraColors.outlineHairline)
        SheetActionRow(label = "Rename", icon = Icons.Outlined.Edit, enabled = !busy, onClick = onRenameTap)
        SheetActionRow(label = "Archive", icon = ArchiveGlyph, enabled = !busy, onClick = onArchiveTap)
        if (error != null) {
            InlineErrorCaption(error)
        }
    }
}

/** Non-interactive title header (caption/secondary treatment, same tier `ComposerOptionsSheet`'s
 * "Session" section label uses) — "Untitled session" fallback identical to `RecentSessionRow`. */
@Composable
private fun SessionHeader(session: SessionSummary, modifier: Modifier = Modifier) {
    Text(
        text = session.title?.takeIf { it.isNotBlank() } ?: "Untitled session",
        style = AuraType.sectionHeader,
        maxLines = 2,
        overflow = TextOverflow.Ellipsis,
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.DrawerRow.sectionHeaderTopPad / 2),
    )
}

@Composable
private fun SheetActionRow(label: String, icon: ImageVector, enabled: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            tint = if (enabled) AuraColors.iconPrimary else AuraColors.textTertiary,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Text(
            text = label,
            style = AuraType.listItem,
            color = if (enabled) AuraColors.textPrimary else AuraColors.textTertiary,
            modifier = Modifier.weight(1f),
        )
    }
}

/**
 * Rename drill-in pane (precedent: `ComposerOptionsSheet`'s Project/Tools panes). The field
 * prefills [initialTitle] fully selected ([TextFieldValue]'s `selection`) so the first keystroke
 * overtypes it — "select-all-friendly" per the task brief — using the same borderless
 * `BasicTextField` + `accentPrimary` cursor idiom as `SettingsScreen`'s `SlimTextField`. Commit is
 * guarded client-side: trimmed, blank/unchanged is a no-op, and [RenameMaxLength] mirrors the
 * server cap (input beyond it is simply not accepted, same behavior a native maxLength would give).
 */
@Composable
private fun RenamePane(
    initialTitle: String,
    busy: Boolean,
    error: String?,
    onBack: () -> Unit,
    onCommit: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var field by remember {
        mutableStateOf(TextFieldValue(text = initialTitle, selection = TextRange(0, initialTitle.length)))
    }
    val trimmed = field.text.trim()
    val canCommit = !busy && trimmed.isNotEmpty() && trimmed != initialTitle.trim()
    // Auto-focus on pane entry so the IME raises and the prefilled full-selection is live —
    // without it the first tap collapses the selection and overtype never happens
    // (device-verified: the field reports focused=false until tapped).
    val focusRequester = remember { FocusRequester() }
    LaunchedEffect(Unit) { focusRequester.requestFocus() }
    // The sheet window dies with the focused field still attached, which strands the IME over
    // whatever is behind (device-verified) — hide it on ANY exit path. The dispose hook alone
    // races the window teardown on the commit path (also device-verified), so [commit] hides
    // eagerly before dispatching; onDispose covers back/scrim exits.
    val keyboard = LocalSoftwareKeyboardController.current
    DisposableEffect(Unit) { onDispose { keyboard?.hide() } }
    val commit: () -> Unit = {
        keyboard?.hide()
        onCommit(trimmed)
    }

    Column(modifier = modifier.fillMaxWidth().padding(bottom = AuraSpacing.Composer.internalPadding)) {
        RenamePaneHeader(
            onBack = onBack,
            onConfirm = commit,
            confirmEnabled = canCommit,
            busy = busy,
        )
        BasicTextField(
            value = field,
            onValueChange = { new -> if (new.text.length <= RenameMaxLength) field = new },
            textStyle = AuraType.listItem.copy(color = AuraColors.textPrimary),
            singleLine = true,
            enabled = !busy,
            cursorBrush = SolidColor(AuraColors.accentPrimary),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { if (canCommit) commit() }),
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight)
                .focusRequester(focusRequester),
        )
        if (error != null) {
            InlineErrorCaption(error)
        }
    }
}

@Composable
private fun RenamePaneHeader(onBack: () -> Unit, onConfirm: () -> Unit, confirmEnabled: Boolean, busy: Boolean, modifier: Modifier = Modifier) {
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
                .clickable(enabled = !busy, onClick = onBack)
                .padding(AuraSpacing.Composer.gapTight)
                .size(AuraSpacing.DrawerRow.iconSize),
        )
        Text(
            text = "Rename",
            style = AuraType.listItem,
            color = AuraColors.textPrimary,
            modifier = Modifier
                .weight(1f)
                .padding(start = AuraSpacing.DrawerRow.iconToLabelGap),
        )
        IconButton(onClick = onConfirm, enabled = confirmEnabled && !busy) {
            if (busy) {
                CircularProgressIndicator(
                    color = AuraColors.accentPrimary,
                    strokeWidth = ConfirmSpinnerStroke,
                    modifier = Modifier.size(ConfirmSpinnerSize),
                )
            } else {
                Icon(
                    imageVector = Icons.Filled.Check,
                    contentDescription = "Save",
                    tint = if (confirmEnabled) AuraColors.accentPrimary else AuraColors.textTertiary,
                    modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
                )
            }
        }
    }
}

@Composable
private fun InlineErrorCaption(text: String, modifier: Modifier = Modifier) {
    Text(
        text = text,
        style = AuraType.caption,
        color = AuraColors.accentError,
        modifier = modifier.padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight),
    )
}

/** Top corners only, same as `ComposerOptionsSheet`/`ModelPickerSheet`. */
private val SheetShape = RoundedCornerShape(topStart = AuraShape.radiusBubble, topEnd = AuraShape.radiusBubble)

/** Server-side title cap (task brief) — input beyond this length is simply not accepted. */
private const val RenameMaxLength = 120

/** No `AuraSpacing` token covers an inline header spinner; same documented gap as
 * `SettingsScreen`'s `ValidateButtonSpinnerSize`/`ValidateButtonSpinnerStroke`. */
private val ConfirmSpinnerSize: Dp = 16.dp
private val ConfirmSpinnerStroke: Dp = 2.dp

/**
 * Hand-ported "archive box" glyph — `material-icons-core` (the app's only icon dependency, no
 * `material-icons-extended`) has no archive glyph at all. Simplified stroked house style, same
 * convention as `ui/chat/ChatIcons`' `PhotoGlyph`/`FileGlyph`/`StopTile`: a lid rectangle, a box
 * body below it, and a small filled pull-tab slot — recognizable as "archive," not a literal
 * Material Symbols path trace.
 */
private val ArchiveGlyph: ImageVector by lazy {
    ImageVector.Builder(name = "Archive", defaultWidth = 24.dp, defaultHeight = 24.dp, viewportWidth = 24f, viewportHeight = 24f)
        .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
            // Lid
            moveTo(3f, 4f)
            horizontalLineToRelative(18f)
            verticalLineToRelative(4f)
            horizontalLineToRelative(-18f)
            close()
        }
        .path(fill = null, stroke = SolidColor(VectorGlyphFill), strokeLineWidth = 1.6f) {
            // Box body (open top - sits flush under the lid's bottom edge)
            moveTo(5f, 8f)
            verticalLineToRelative(12f)
            horizontalLineToRelative(14f)
            verticalLineToRelative(-12f)
        }
        .path(fill = SolidColor(VectorGlyphFill)) {
            // Pull-tab slot
            moveTo(9.5f, 12f)
            horizontalLineToRelative(5f)
            verticalLineToRelative(1.4f)
            horizontalLineToRelative(-5f)
            close()
        }
        .build()
}
