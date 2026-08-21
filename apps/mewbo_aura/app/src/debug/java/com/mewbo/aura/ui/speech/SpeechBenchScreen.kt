package com.mewbo.aura.ui.speech

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.model.SpeechDirection
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * The bench's whole surface: a field, an engine list, a speak button, and a result block.
 *
 * Deliberately a plain scrolling column rather than the settings screen's sectioned cards — this
 * is a debug instrument, and every one of its four parts must be visible in one screenshot without
 * expanding anything. It still spends only `AuraTheme` tokens, because a debug surface rendering
 * off-token is how a literal escapes into the app later.
 *
 * **The cloud mark is the settings convention, reused verbatim** — [SpeechCatalog.cloudLabel] plus
 * a `Cloud` glyph, the same two signals `SpeechEnginePickerSheet` renders. One place attaches the
 * mark, so a bench row and a settings row can never disagree about whether audio leaves the phone.
 */
@Composable
fun SpeechBenchScreen(
    state: SpeechBenchState,
    onTextChange: (String) -> Unit,
    onSelectEngine: (String) -> Unit,
    onManualEngineChange: (String) -> Unit,
    onSpeak: () -> Unit,
    onStop: () -> Unit,
    onReloadCatalog: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val serverOptions = state.catalog?.serverOptions(SpeechDirection.TextToSpeech).orEmpty()

    Scaffold(containerColor = AuraColors.surfaceCanvas, modifier = modifier) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(vertical = AuraSpacing.Composer.internalPadding),
        ) {
            Text(
                text = "Speech bench",
                style = AuraType.sectionHeader,
                color = AuraColors.textPrimary,
                modifier = Modifier.padding(horizontal = AuraSpacing.screenGutter),
            )

            BenchTextField(text = state.text, onTextChange = onTextChange)

            HorizontalDivider(color = AuraColors.outlineHairline)
            EngineListHeader(
                loading = state.catalogLoading,
                failed = state.catalogFailed,
                serverCount = serverOptions.size,
                onReload = onReloadCatalog,
            )

            EngineRow(
                label = SpeechCatalog.ON_DEVICE_LABEL,
                // Names the class the leg resolved to, because "on device" covers two very
                // different implementations and an AOSP result is unreadable without knowing which.
                caption = state.onDeviceEngineName,
                glyph = Icons.Filled.PhoneAndroid,
                selected = SpeechCatalog.isOnDevice(state.selectedEngine),
                onClick = { onSelectEngine(SpeechCatalog.ON_DEVICE) },
            )
            serverOptions.forEach { option ->
                EngineRow(
                    label = SpeechCatalog.cloudLabel(option.label),
                    caption = option.id,
                    glyph = Icons.Filled.Cloud,
                    selected = option.id == state.selectedEngine,
                    onClick = { onSelectEngine(option.id) },
                )
            }
            ManualEngineRow(
                value = state.manualEngine,
                selected = state.manualEngine.isNotBlank() && state.manualEngine.trim() == state.selectedEngine,
                onValueChange = onManualEngineChange,
            )

            HorizontalDivider(color = AuraColors.outlineHairline)
            ChunkPlanBlock(state = state)
            HorizontalDivider(color = AuraColors.outlineHairline)
            SpeakControls(state = state, onSpeak = onSpeak, onStop = onStop)
            ResultBlock(state = state)
        }
    }
}

@Composable
private fun BenchTextField(text: String, onTextChange: (String) -> Unit, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.internalPadding),
    ) {
        Text("Text to speak", style = AuraType.caption, color = AuraColors.textSecondary)
        BasicTextField(
            value = text,
            onValueChange = onTextChange,
            textStyle = AuraType.listItem.copy(color = AuraColors.textPrimary),
            cursorBrush = SolidColor(AuraColors.accentPrimary),
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = AuraSpacing.Settings.rowMinHeight)
                .padding(top = AuraSpacing.Settings.captionGap),
        )
    }
}

@Composable
private fun EngineListHeader(
    loading: Boolean,
    failed: Boolean,
    serverCount: Int,
    onReload: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .padding(start = AuraSpacing.screenGutter, top = AuraSpacing.Composer.internalPadding),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text("Engine", style = AuraType.caption, color = AuraColors.textSecondary)
            Text(
                // States what is KNOWN, and the three cases are genuinely different facts. A
                // failed fetch is not "no engines"; and an EMPTY list is not "the gateway serves
                // nothing" — this deployment advertises zero models while synthesizing fine,
                // because model discovery is refused for the runtime credential. Saying "0
                // engines" there would blame the wrong component, so it points at the id field.
                text = when {
                    loading -> "Loading the gateway's engines…"
                    failed -> "Couldn't load gateway engines"
                    serverCount == 0 -> "The gateway advertised no models — type an id below"
                    else -> "$serverCount from the gateway, plus this phone"
                },
                style = AuraType.caption,
                color = if (failed) AuraColors.accentError else AuraColors.textTertiary,
            )
        }
        TextButton(onClick = onReload, enabled = !loading) {
            Icon(
                imageVector = Icons.Filled.Refresh,
                contentDescription = "Reload engines",
                tint = AuraColors.iconPrimary,
                modifier = Modifier.size(AuraSpacing.ActionRow.iconSize),
            )
        }
    }
}

@Composable
private fun EngineRow(
    label: String,
    caption: String,
    glyph: ImageVector,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .height(AuraSpacing.DrawerRow.height)
            .clickable(onClick = onClick)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Icon(
            imageVector = glyph,
            contentDescription = null,
            tint = if (selected) AuraColors.accentPrimary else AuraColors.iconPrimary,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = label,
                style = AuraType.listItem,
                color = if (selected) AuraColors.accentPrimary else AuraColors.textPrimary,
            )
            Text(text = caption, style = AuraType.caption, color = AuraColors.textSecondary)
        }
        if (selected) {
            Text("Selected", style = AuraType.caption, color = AuraColors.accentPrimary)
        }
    }
}

/**
 * A gateway model id typed by hand — the escape hatch for a deployment that advertises none.
 *
 * It carries the SAME cloud glyph and [SpeechCatalog.cloudLabel] mark as an enumerated row,
 * because the privacy fact the mark reports is identical: a typed id is still a server engine and
 * still sends the text off the phone. Marking only the rows that arrived from a catalogue would
 * make the mark a statement about provenance instead of about where the audio goes.
 */
@Composable
private fun ManualEngineRow(
    value: String,
    selected: Boolean,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = AuraSpacing.DrawerRow.height)
            .padding(horizontal = AuraSpacing.screenGutter),
    ) {
        Icon(
            imageVector = Icons.Filled.Cloud,
            contentDescription = null,
            tint = if (selected) AuraColors.accentPrimary else AuraColors.iconPrimary,
            modifier = Modifier.size(AuraSpacing.DrawerRow.iconSize),
        )
        Spacer(Modifier.width(AuraSpacing.DrawerRow.iconToLabelGap))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = SpeechCatalog.cloudLabel("Gateway model id"),
                style = AuraType.caption,
                color = AuraColors.textSecondary,
            )
            BasicTextField(
                value = value,
                onValueChange = onValueChange,
                singleLine = true,
                textStyle = AuraType.listItem.copy(
                    color = if (selected) AuraColors.accentPrimary else AuraColors.textPrimary,
                ),
                cursorBrush = SolidColor(AuraColors.accentPrimary),
                decorationBox = { field ->
                    if (value.isEmpty()) {
                        Text("supertonic-3", style = AuraType.listItem, color = AuraColors.textTertiary)
                    }
                    field()
                },
                modifier = Modifier.fillMaxWidth(),
            )
        }
        if (selected) {
            Text("Selected", style = AuraType.caption, color = AuraColors.accentPrimary)
        }
    }
}

@Composable
private fun SpeakControls(
    state: SpeechBenchState,
    onSpeak: () -> Unit,
    onStop: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.internalPadding),
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight)) {
            Button(
                onClick = onSpeak,
                // The selected engine reporting itself unavailable is the AOSP on-device case, and
                // the button must refuse rather than accept a press that goes nowhere silently.
                enabled = state.engineAvailable && !state.speaking && state.text.isNotBlank(),
                shape = AuraShape.radiusPill,
                colors = ButtonDefaults.buttonColors(
                    containerColor = AuraColors.accentPrimary,
                    contentColor = AuraColors.accentOnAccent,
                    disabledContainerColor = AuraColors.accentMuted,
                    disabledContentColor = AuraColors.accentOnAccent,
                ),
            ) {
                if (state.speaking) {
                    CircularProgressIndicator(
                        color = AuraColors.accentOnAccent,
                        strokeWidth = SpinnerStroke,
                        modifier = Modifier.size(SpinnerSize),
                    )
                    Spacer(Modifier.width(AuraSpacing.Composer.gapTight))
                }
                Text(if (state.speaking) "Speaking…" else "Speak", style = AuraType.listItem)
            }
            TextButton(onClick = onStop, enabled = state.speaking) {
                Text("Stop", style = AuraType.listItem, color = AuraColors.textPrimary)
            }
        }
        if (!state.engineAvailable) {
            Text(
                // Says which engine is unavailable and what to do, rather than "unavailable" —
                // the on-device leg has no TTS engine on AOSP, and that is a device fact, not a bug.
                text = if (SpeechCatalog.isOnDevice(state.selectedEngine)) {
                    "No usable text-to-speech engine on this device. Pick a gateway engine, " +
                        "or run this on hardware with a TTS engine installed."
                } else {
                    "This engine reports itself unavailable."
                },
                style = AuraType.caption,
                color = AuraColors.textSecondary,
                modifier = Modifier.padding(top = AuraSpacing.Composer.gapTight),
            )
        }
    }
}

/**
 * The chunk plan: how this text will be split, and what each piece cost once spoken.
 *
 * **The chunking is CLIENT-side, and the screen says so.** `SentenceChunker` splits the text and
 * the app POSTs one `/api/speech/synthesize` per chunk; the server synthesizes whatever it is
 * handed and does no splitting of its own. Anyone reading a per-chunk latency here would otherwise
 * reasonably assume the backend chose the boundaries, and tune the wrong component.
 *
 * The character count is of the SPOKEN text, after the chunker's markdown strip — the same string
 * that goes on the wire, and the one the server's `max_text_chars` cap is measured against.
 */
@Composable
private fun ChunkPlanBlock(state: SpeechBenchState, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.internalPadding),
        verticalArrangement = Arrangement.spacedBy(AuraSpacing.Settings.captionGap),
    ) {
        Text("Chunk plan", style = AuraType.caption, color = AuraColors.textSecondary)
        Text(
            text = "${state.chunks.size} chunk(s) — split on THIS device by SentenceChunker, one " +
                "request each. The server does not chunk; it synthesizes what it is sent.",
            style = AuraType.caption,
            color = AuraColors.textTertiary,
        )
        state.chunks.forEach { chunk -> ChunkRow(chunk) }
        if (state.chunks.isEmpty()) {
            Text("Nothing to speak.", style = AuraType.caption, color = AuraColors.textTertiary)
        }
    }
}

@Composable
private fun ChunkRow(chunk: SpeechChunk, modifier: Modifier = Modifier) {
    Column(modifier = modifier.padding(top = AuraSpacing.Composer.gapTight)) {
        Row {
            Text(
                text = "#${chunk.index + 1}",
                style = AuraType.caption,
                color = AuraColors.textTertiary,
                modifier = Modifier.width(ChunkIndexWidth),
            )
            Text(
                text = "${chunk.charCount} chars",
                style = AuraType.caption,
                color = if (chunk.failed) AuraColors.accentError else AuraColors.textSecondary,
                modifier = Modifier.weight(1f),
            )
            // Absent until this chunk has been spoken; absent FOREVER on the on-device leg, which
            // makes no gateway call at all. Neither is a failure, so neither renders as one.
            chunk.call?.let { call ->
                Text(
                    text = "${call.networkMillis}ms · ${call.audioBytes}B",
                    style = AuraType.caption,
                    color = if (call.failure != null) AuraColors.accentError else AuraColors.accentSuccess,
                )
            }
        }
        Text(
            text = chunk.text,
            style = AuraType.caption,
            color = AuraColors.textPrimary,
            modifier = Modifier.padding(start = ChunkIndexWidth),
        )
        chunk.call?.failure?.let { failure ->
            Text(
                text = failure,
                style = AuraType.caption,
                color = AuraColors.accentError,
                modifier = Modifier.padding(start = ChunkIndexWidth),
            )
        }
    }
}

/**
 * What happened, in the terms the bench was built to answer: did it speak, how long until audio,
 * how long in total, how many bytes came back, and — on a failure — what the transport said.
 *
 * A failure is never rendered as a bare "failed": the whole reason for [RecordingSpeechGateway] is
 * that `SynthEvent.Error` carries no reason, and a bench that echoes that back has measured
 * nothing.
 */
@Composable
private fun ResultBlock(state: SpeechBenchState, modifier: Modifier = Modifier) {
    val outcome = state.outcome
    if (outcome is SpeechBenchOutcome.Idle) return

    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter)
            .semantics { contentDescription = "Speech bench result" },
        verticalArrangement = Arrangement.spacedBy(AuraSpacing.Settings.captionGap),
    ) {
        HorizontalDivider(color = AuraColors.outlineHairline)
        Spacer(Modifier.height(AuraSpacing.Composer.gapTight))
        when (outcome) {
            is SpeechBenchOutcome.Idle -> Unit
            is SpeechBenchOutcome.Speaking -> ResultLine(
                "Speaking",
                outcome.firstAudioMillis?.let { "audio started after ${it}ms" } ?: "waiting for audio…",
                AuraColors.textPrimary,
            )
            is SpeechBenchOutcome.Spoke -> {
                ResultLine("Spoke", "${outcome.totalMillis}ms end to end", AuraColors.accentSuccess)
                outcome.firstAudioMillis?.let {
                    ResultLine("Time to first audio", "${it}ms", AuraColors.textSecondary)
                }
                TotalsLines(state)
                BoundaryNotes(state)
            }
            is SpeechBenchOutcome.Failed -> {
                ResultLine("Failed", "after ${outcome.totalMillis}ms", AuraColors.accentError)
                TotalsLines(state)
                outcome.call?.let { ResultLine("Model", it.modelId, AuraColors.textSecondary) }
                outcome.call?.failure?.let { ResultLine("Error", it, AuraColors.accentError) }
                if (outcome.call == null) {
                    // No gateway call recorded ⇒ the failure happened on the on-device leg, which
                    // never touches the network. Saying so beats an empty failure block.
                    ResultLine("Where", "on-device engine, no gateway call made", AuraColors.textSecondary)
                }
            }
        }
    }
}

/** Summed across chunks, so the per-chunk rows above and this total cannot disagree. */
@Composable
private fun TotalsLines(state: SpeechBenchState) {
    val calls = state.chunks.mapNotNull { it.call }
    if (calls.isEmpty()) return
    ResultLine("Synthesis total", "${calls.sumOf { it.networkMillis }}ms over ${calls.size} call(s)", AuraColors.textSecondary)
    ResultLine("Audio total", "${calls.sumOf { it.audioBytes }} bytes", AuraColors.textSecondary)
}

/**
 * What each number above INCLUDES — stated rather than assumed, because the same figure means
 * different things on a cold and a warm call and the bench cannot tell the two apart.
 */
@Composable
private fun BoundaryNotes(state: SpeechBenchState) {
    if (state.chunks.none { it.call != null }) return
    Spacer(Modifier.height(AuraSpacing.Settings.captionGap))
    Text(
        text = "Each chunk time is request → last byte: connection setup, server synthesis and " +
            "transfer together. The bench cannot separate them (that would need an OkHttp " +
            "listener on the shared production client) and cannot tell a COLD call from a warm " +
            "one — a first call after a restart pays model setup and runs several times longer.",
        style = AuraType.caption,
        color = AuraColors.textTertiary,
    )
}

@Composable
private fun ResultLine(label: String, value: String, valueColor: Color) {
    Row(modifier = Modifier.fillMaxWidth()) {
        Text(
            text = label,
            style = AuraType.caption,
            color = AuraColors.textTertiary,
            modifier = Modifier.width(ResultLabelWidth),
        )
        Text(text = value, style = AuraType.caption, color = valueColor, modifier = Modifier.weight(1f))
    }
}

/** No token covers a two-column debug readout's label gutter or an inline button spinner, and
 * minting one in `ui/theme/` for a debug-only screen would put a debug concern in the design
 * system. Flagged here rather than smuggled — the same call `SettingsScreen`'s
 * `ValidateButtonSpinnerSize` and `ErrorCard`'s `WarningGlyphSize` already document. */
private val ResultLabelWidth: Dp = 148.dp
private val ChunkIndexWidth: Dp = 32.dp
private val SpinnerSize: Dp = 16.dp
private val SpinnerStroke: Dp = 2.dp
