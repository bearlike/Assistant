package com.mewbo.aura.ui.apps

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.mewbo.aura.data.model.AppFreshness
import com.mewbo.aura.data.model.AppTrigger
import com.mewbo.aura.data.model.PipelineLiveness
import com.mewbo.aura.ui.common.ErrorCard
import com.mewbo.aura.ui.sessions.RelativeTime
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * App detail: the [AppWebView] render + a minimal health row (design spec §4D). Triggers stay
 * collapsed by default (compact design language: no error residue, no chrome unless asked for) and
 * expand into pause/resume rows on tap.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppDetailScreen(
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: AppDetailViewModel = hiltViewModel(),
) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    var triggersExpanded by remember { mutableStateOf(false) }
    var pipelinesExpanded by remember { mutableStateOf(false) }

    Scaffold(
        modifier = modifier,
        containerColor = AuraColors.surfaceCanvas,
        topBar = {
            TopAppBar(
                title = {
                    Text(
                        text = (uiState as? AppDetailUiState.Loaded)?.detail?.title ?: "App",
                        style = AuraType.titleBar,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back", tint = AuraColors.iconPrimary)
                    }
                },
                actions = {
                    IconButton(onClick = viewModel::refresh) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Refresh", tint = AuraColors.iconPrimary)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = AuraColors.surfaceCanvas),
            )
        },
    ) { padding ->
        when (val state = uiState) {
            is AppDetailUiState.Loading -> Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center,
            ) {
                CircularProgressIndicator(color = AuraColors.accentPrimary)
            }
            is AppDetailUiState.Error -> Box(modifier = Modifier.fillMaxSize().padding(padding).padding(AuraSpacing.screenGutter)) {
                ErrorCard(reason = state.message, onRetry = viewModel::refresh)
            }
            is AppDetailUiState.Loaded -> {
                val triggers = state.health?.triggers.orEmpty()
                val pipelines = state.health?.pipelines.orEmpty()
                Column(modifier = Modifier.fillMaxSize().padding(padding)) {
                    AppHealthRow(
                        status = state.detail.status,
                        freshness = state.health?.freshness,
                        triggers = triggers,
                        triggersExpanded = triggersExpanded,
                        onToggleTriggers = { triggersExpanded = !triggersExpanded },
                    )
                    AnimatedVisibility(visible = triggersExpanded && triggers.isNotEmpty()) {
                        Column {
                            triggers.forEach { trigger ->
                                AppTriggerRow(
                                    trigger = trigger,
                                    onTogglePause = { viewModel.setTriggerPaused(trigger.id, paused = trigger.status != "paused") },
                                )
                            }
                        }
                    }
                    // Additive — an older server simply omits `pipelines`, so
                    // this whole section degrades to absent rather than an empty/broken row.
                    if (pipelines.isNotEmpty()) {
                        AppPipelinesRow(
                            pipelines = pipelines,
                            expanded = pipelinesExpanded,
                            onToggle = { pipelinesExpanded = !pipelinesExpanded },
                        )
                        AnimatedVisibility(visible = pipelinesExpanded) {
                            Column {
                                pipelines.forEach { pipeline -> AppPipelineRow(pipeline = pipeline) }
                            }
                        }
                    }
                    HorizontalDivider(color = AuraColors.outlineHairline)
                    AppWebView(
                        detail = state.detail,
                        token = state.token,
                        apiBase = viewModel.apiBase,
                        modifier = Modifier.fillMaxWidth().weight(1f),
                    )
                }
            }
        }
    }
}

@Composable
private fun AppHealthRow(
    status: String,
    freshness: AppFreshness?,
    triggers: List<AppTrigger>,
    triggersExpanded: Boolean,
    onToggleTriggers: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val activeCount = triggers.count { it.isActive }
    // "Never refreshed" / stale reads in the same error tint `AppStatusDot` already uses for a
    // "broken" app — Aura has no separate warning token, so this reuses the existing problem color
    // rather than inventing one (see the mirroring note on `scheduleLabel` below).
    val freshnessWarn = freshness == null || freshness.lastSuccessAt == null || freshness.stale
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clickable(enabled = triggers.isNotEmpty(), onClick = onToggleTriggers)
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            AppStatusDot(status = status)
            Spacer(Modifier.width(AuraSpacing.Composer.internalPadding))
            Text(
                text = freshnessLabel(freshness),
                style = AuraType.caption,
                color = if (freshnessWarn) AuraColors.accentError else AuraColors.textSecondary,
                modifier = Modifier.weight(1f),
            )
            if (triggers.isNotEmpty()) {
                Text(text = "$activeCount active", style = AuraType.caption)
                Icon(
                    imageVector = if (triggersExpanded) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
                    contentDescription = if (triggersExpanded) "Hide triggers" else "Show triggers",
                    tint = AuraColors.textSecondary,
                )
            }
        }
        // Independent of the last-success axis above — mirrors the console's two-chip freshness row
        // (`AppFreshness.tsx`): "no refresh schedule" is its own warning that can co-occur with
        // "never refreshed".
        Text(
            text = scheduleLabel(freshness),
            style = AuraType.caption,
            color = if (freshness?.nextFireAt == null) AuraColors.accentError else AuraColors.textSecondary,
        )
    }
}

/** [freshness]'s `nextFireAt` reads as "no refresh schedule" whenever null — same honest-by-default
 * stance as the console's `AppFreshness.tsx`: a pure on-demand app legitimately has no next fire
 * either, so this can read as a warning even when that's by design. Console accepts that tradeoff
 * (see its KDoc), and this mirrors it rather than trying to distinguish "on demand" here without the
 * per-pipeline detail [AppPipelinesRow] already carries. */
private fun scheduleLabel(freshness: AppFreshness?): String =
    freshness?.nextFireAt?.let { "Next refresh ${RelativeTime.format(it)}" } ?: "No refresh schedule"

@Composable
private fun AppTriggerRow(trigger: AppTrigger, onTogglePause: () -> Unit, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(text = trigger.kind, style = AuraType.listItem, color = AuraColors.textPrimary)
            Text(text = triggerScheduleLabel(trigger), style = AuraType.caption)
        }
        if (trigger.isActive) {
            IconButton(onClick = onTogglePause) {
                Icon(
                    imageVector = if (trigger.status == "paused") Icons.Filled.PlayArrow else Icons.Filled.Pause,
                    contentDescription = if (trigger.status == "paused") "Resume" else "Pause",
                    tint = AuraColors.iconPrimary,
                )
            }
        }
    }
}

private fun triggerScheduleLabel(trigger: AppTrigger): String = when {
    trigger.status == "paused" -> "Paused"
    trigger.nextFireAt != null -> "Next ${RelativeTime.format(trigger.nextFireAt)}"
    trigger.lastFiredAt != null -> "Last fired ${RelativeTime.format(trigger.lastFiredAt)}"
    else -> trigger.status
}

/** Collapsed summary row for [AppPipelineRow]'s list — same clickable-row-with-chevron idiom
 * [AppHealthRow] uses for triggers, reused here rather than inventing a second expand pattern. */
@Composable
private fun AppPipelinesRow(
    pipelines: List<PipelineLiveness>,
    expanded: Boolean,
    onToggle: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val unscheduled = pipelines.count { !it.armed && !it.onDemand }
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clickable(onClick = onToggle)
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(text = "Pipelines", style = AuraType.caption, color = AuraColors.textSecondary, modifier = Modifier.weight(1f))
        if (unscheduled > 0) {
            Text(text = "$unscheduled unscheduled", style = AuraType.caption, color = AuraColors.accentError)
            Spacer(Modifier.width(AuraSpacing.Composer.internalPadding))
        }
        Icon(
            imageVector = if (expanded) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore,
            contentDescription = if (expanded) "Hide pipelines" else "Show pipelines",
            tint = AuraColors.textSecondary,
        )
    }
}

@Composable
private fun AppPipelineRow(pipeline: PipelineLiveness, modifier: Modifier = Modifier) {
    val (text, warn) = pipelineLivenessLabel(pipeline)
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.screenGutter, vertical = AuraSpacing.Composer.gapTight),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = pipeline.name,
            style = AuraType.listItem,
            color = AuraColors.textPrimary,
            modifier = Modifier.weight(1f),
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        Text(text = text, style = AuraType.caption, color = if (warn) AuraColors.accentError else AuraColors.textSecondary)
    }
}

/**
 * Humane per-pipeline liveness text, mirroring the console's `describePipelineLiveness`
 * (`components/apps/pipelineSchedule.ts` — Kotlin/TS can't share code, so this is a parallel port,
 * not a divergence): a cron schedule reads as a plain-language cadence, `time.at` as a relative
 * one-off, an on-demand pipeline reads "On demand" (never a warning — it's a deliberate design
 * choice), and no schedule at all keeps the wave-1 "No refresh schedule" warning. A declared
 * schedule that isn't currently armed still warns — that's the actual bug this guards against.
 */
private fun pipelineLivenessLabel(pipeline: PipelineLiveness): Pair<String, Boolean> {
    val schedule = pipeline.schedule
    if (schedule != null) {
        val base = when (schedule.kind) {
            "time.cron" -> describeCron(schedule.expr)?.let { "Refreshes $it" } ?: schedule.expr
            "time.at" -> "Runs ${RelativeTime.format(schedule.expr)}"
            else -> schedule.expr
        }
        return if (pipeline.armed) base to false else "$base · not armed" to true
    }
    if (pipeline.onDemand) return "On demand" to false
    return "No refresh schedule" to true
}

/** Best-effort cron to plain language for the common shapes an agent actually emits (every N
 * minutes, every N hours, hourly, daily); anything else (day-of-month/weekday fields, non-numeric
 * minute/hour) returns null so the caller falls back to the raw expression rather than guessing
 * wrong. Deliberately not a cron library, per the console's identical helper. */
private fun describeCron(cron: String): String? {
    val parts = cron.trim().split(Regex("\\s+"))
    if (parts.size != 5) return null
    val (minute, hour, dayOfMonth, month, dayOfWeek) = parts
    if (dayOfMonth != "*" || month != "*" || dayOfWeek != "*") return null

    fun everyN(field: String): Int? = Regex("^\\*/(\\d+)$").find(field)?.groupValues?.get(1)?.toIntOrNull()

    val everyMinute = everyN(minute)
    if (everyMinute != null && hour == "*") {
        return if (everyMinute == 1) "every minute" else "every $everyMinute min"
    }
    val everyHour = everyN(hour)
    if (minute.toIntOrNull() != null && everyHour != null) {
        return if (everyHour == 1) "hourly" else "every $everyHour hours"
    }
    if (minute.toIntOrNull() != null && hour == "*") return "hourly"
    if (minute.toIntOrNull() != null && hour.toIntOrNull() != null) return "daily"
    return null
}
