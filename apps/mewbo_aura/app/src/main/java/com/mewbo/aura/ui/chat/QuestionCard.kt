package com.mewbo.aura.ui.chat

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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.HelpOutline
import androidx.compose.material.icons.filled.CheckBox
import androidx.compose.material.icons.filled.CheckBoxOutlineBlank
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.RadioButtonChecked
import androidx.compose.material.icons.filled.RadioButtonUnchecked
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshots.SnapshotStateList
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.style.TextOverflow
import com.mewbo.aura.data.api.QuestionAnswerItemDto
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.QuestionResolution
import com.mewbo.aura.data.model.UiAnswer
import com.mewbo.aura.data.model.UiQuestion
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraShape
import com.mewbo.aura.ui.theme.AuraSpacing
import com.mewbo.aura.ui.theme.AuraType

/**
 * The ask-user question card (ask-user questions) — the human-in-the-loop clarification surface
 * the agent blocks on. Renders 1-4 questions as one group with a single Submit. Two lifetimes, keyed
 * off [ChatItem.Question.resolution]:
 *
 * - **Pending** (`resolution == null`): interactive. Per question a header chip, the question text,
 *   tappable option rows (radio for single-select, checkboxes for multi-select) plus an ever-present
 *   free-text "Other", and ONE Submit for the group. Options and free text are mutually exclusive per
 *   question (the wire is `selected_indexes` XOR `text`), enforced here — tapping an option drops the
 *   Other text, typing Other clears the selection.
 * - **Settled** (`resolution != null`): read-only. [QuestionResolution.Answered] shows the chosen
 *   answers (and "answered on <surface>" when another surface answered — the card settles the same way
 *   regardless of WHO answered); [QuestionResolution.Dismissed] dims to "no longer waiting" with NO
 *   error residue (DESIGN.md §6), which is how declined/interrupted/cancelled land.
 *
 * The AUTHORITATIVE settle is always the `user_question_answered` event flipping `resolution`
 * ([com.mewbo.aura.data.model.TranscriptReducer]); [onSubmit]'s `onResult(false)` (a genuine POST
 * failure — a 404/409 answered-elsewhere is NOT one) only re-enables the card and toasts via
 * [onNotice]. Wears the same `surfaceSelected` / `radiusBubble` / gutter anatomy as
 * `ToolActionCard` (a payload surface, not a glance chip).
 */
@Composable
fun QuestionCard(
    item: ChatItem.Question,
    sessionEnded: Boolean,
    onSubmit: (callId: String, callToken: String, answers: List<QuestionAnswerItemDto>, onResult: (Boolean) -> Unit) -> Unit,
    onNotice: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        color = AuraColors.surfaceSelected,
        shape = RoundedCornerShape(AuraShape.radiusBubble),
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = AuraSpacing.AssistantText.gutter),
    ) {
        Column(
            modifier = Modifier.padding(
                horizontal = AuraSpacing.Composer.internalPadding,
                vertical = AuraSpacing.ToolCard.paddingVertical,
            ),
        ) {
            CardHeader(resolution = item.resolution)
            Spacer(modifier = Modifier.height(AuraSpacing.ToolCard.headerToContentGap))
            when (val resolution = item.resolution) {
                null -> PendingQuestionGroup(item = item, sessionEnded = sessionEnded, onSubmit = onSubmit, onNotice = onNotice)
                is QuestionResolution.Answered -> AnsweredQuestionGroup(questions = item.questions, resolution = resolution)
                is QuestionResolution.Dismissed -> Unit // the header already states it's closed
            }
        }
    }
}

/** The card-level identity row (glyph + status label), mirroring `ToolActionCard`'s header anatomy. */
@Composable
private fun CardHeader(resolution: QuestionResolution?) {
    val (glyph, label) = when (resolution) {
        null -> Icons.AutoMirrored.Filled.HelpOutline to "A question for you"
        is QuestionResolution.Answered ->
            Icons.Filled.CheckCircle to (resolution.answeredVia.settledLabel())
        is QuestionResolution.Dismissed -> Icons.AutoMirrored.Filled.HelpOutline to "No longer waiting for an answer"
    }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(AuraSpacing.ToolCard.headerIconGap),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Icon(
            imageVector = glyph,
            contentDescription = null,
            tint = AuraColors.textSecondary,
            modifier = Modifier.size(AuraSpacing.ToolCard.headerIconSize),
        )
        Text(text = label, style = AuraType.chipLabel, color = AuraColors.textSecondary, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

/** "You answered" when this device answered (or the surface is unknown), else "Answered on <surface>". */
private fun String?.settledLabel(): String = when {
    this == null || this == "android" -> "You answered"
    else -> "Answered on $this"
}

@Composable
private fun PendingQuestionGroup(
    item: ChatItem.Question,
    sessionEnded: Boolean,
    onSubmit: (callId: String, callToken: String, answers: List<QuestionAnswerItemDto>, onResult: (Boolean) -> Unit) -> Unit,
    onNotice: (String) -> Unit,
) {
    // One choice-state per question, keyed on the callId so a fresh question resets its inputs while
    // recomposition (streaming deltas elsewhere) preserves them. This is the card's local interactive
    // state — the reducer's ChatItem carries only the immutable question spec + the settled outcome.
    val choices = remember(item.callId) { item.questions.map { QuestionChoice() } }
    var submitting by remember(item.callId) { mutableStateOf(false) }

    item.questions.forEachIndexed { index, question ->
        if (index > 0) Spacer(modifier = Modifier.height(AuraSpacing.Composer.internalPadding))
        QuestionBlock(
            question = question,
            choice = choices[index],
            enabled = !submitting && !sessionEnded,
        )
    }

    val answers = choices.mapIndexed { i, choice -> choice.answerItemOrNull(item.questions[i]) }
    val allAnswered = answers.all { it != null }

    Spacer(modifier = Modifier.height(AuraSpacing.Composer.internalPadding))
    SubmitButton(
        enabled = allAnswered && !submitting && !sessionEnded,
        submitting = submitting,
        onClick = {
            submitting = true
            onSubmit(item.callId, item.callToken, answers.filterNotNull()) { success ->
                // Success (or answered-elsewhere) leaves the spinner up — the user_question_answered
                // event flips `resolution` and swaps this whole branch out for the settled render.
                // A genuine failure re-enables the card and toasts.
                if (!success) {
                    submitting = false
                    onNotice("Couldn't submit your answer")
                }
            }
        },
    )
}

/** One question inside the pending group: header chip, question text, option rows + free-text "Other". */
@Composable
private fun QuestionBlock(question: UiQuestion, choice: QuestionChoice, enabled: Boolean) {
    HeaderChip(header = question.header)
    Spacer(modifier = Modifier.height(AuraSpacing.Composer.gapTight))
    Text(text = question.question, style = AuraType.bodyMessage, color = AuraColors.textPrimary)
    Spacer(modifier = Modifier.height(AuraSpacing.ToolCard.headerToContentGap))

    question.options.forEachIndexed { i, option ->
        val selected = !choice.otherActive && i in choice.indexes
        OptionRow(
            label = option.label,
            description = option.description,
            selected = selected,
            multiSelect = question.multiSelect,
            enabled = enabled,
            onClick = { choice.selectOption(i, question.multiSelect) },
        )
    }
    OtherField(
        value = choice.otherText,
        active = choice.otherActive,
        // A pure free-text question (no options) has no "Other" alternative — the field IS the answer.
        placeholder = if (question.options.isEmpty()) "Your answer" else "Other",
        enabled = enabled,
        onValueChange = choice::typeOther,
    )
}

/** The short topic chip above each question (core: "Very short topic chip, e.g. 'Auth method'"). */
@Composable
private fun HeaderChip(header: String) {
    Surface(color = AuraColors.surfaceInput, shape = AuraShape.radiusPill) {
        Text(
            text = header,
            style = AuraType.chipLabel,
            color = AuraColors.textSecondary,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = AuraSpacing.Composer.gapTight, vertical = AuraSpacing.ToolCard.headerIconGap),
        )
    }
}

@Composable
private fun OptionRow(
    label: String,
    description: String?,
    selected: Boolean,
    multiSelect: Boolean,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val glyph: ImageVector = when {
        multiSelect && selected -> Icons.Filled.CheckBox
        multiSelect -> Icons.Filled.CheckBoxOutlineBlank
        selected -> Icons.Filled.RadioButtonChecked
        else -> Icons.Filled.RadioButtonUnchecked
    }
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
        modifier = Modifier
            .fillMaxWidth()
            .minimumInteractiveComponentSize()
            .clickable(enabled = enabled, onClick = onClick),
    ) {
        Icon(
            imageVector = glyph,
            contentDescription = null,
            tint = if (selected) AuraColors.accentPrimary else AuraColors.textSecondary,
            modifier = Modifier.size(AuraSpacing.Composer.iconSize),
        )
        Column {
            Text(text = label, style = AuraType.bodyMessage, color = AuraColors.textPrimary)
            if (!description.isNullOrBlank()) {
                Text(text = description, style = AuraType.caption, color = AuraColors.textSecondary)
            }
        }
    }
}

/** The ever-present free-text answer. Typing switches this question to a text answer (clearing any
 * option selection — the wire is XOR); a blank field yields no answer. */
@Composable
private fun OtherField(
    value: String,
    active: Boolean,
    placeholder: String,
    enabled: Boolean,
    onValueChange: (String) -> Unit,
) {
    Surface(
        color = AuraColors.surfaceInput,
        shape = RoundedCornerShape(AuraShape.radiusThumb),
        modifier = Modifier.fillMaxWidth(),
    ) {
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            enabled = enabled,
            textStyle = AuraType.bodyMessage.copy(color = AuraColors.textPrimary),
            cursorBrush = SolidColor(AuraColors.accentPrimary),
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = AuraSpacing.Composer.internalPadding, vertical = AuraSpacing.UserBubble.paddingVertical),
            decorationBox = { inner ->
                if (value.isEmpty()) {
                    Text(text = placeholder, style = AuraType.bodyMessage, color = if (active) AuraColors.textSecondary else AuraColors.textTertiary)
                }
                inner()
            },
        )
    }
}

@Composable
private fun SubmitButton(enabled: Boolean, submitting: Boolean, onClick: () -> Unit) {
    Surface(
        color = if (enabled) AuraColors.accentPrimary else AuraColors.accentMuted,
        shape = AuraShape.radiusPill,
        modifier = Modifier
            .minimumInteractiveComponentSize()
            .clickable(enabled = enabled, onClick = onClick),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
            modifier = Modifier.padding(horizontal = AuraSpacing.Composer.internalPadding, vertical = AuraSpacing.Composer.gapTight),
        ) {
            if (submitting) {
                CircularProgressIndicator(
                    color = AuraColors.accentOnAccent,
                    strokeWidth = AuraShape.hairlineWidth,
                    modifier = Modifier.size(AuraSpacing.ActionRow.iconSize),
                )
            }
            Text(
                text = if (submitting) "Submitting…" else "Submit",
                style = AuraType.listItem,
                color = if (enabled || submitting) AuraColors.accentOnAccent else AuraColors.textSecondary,
            )
        }
    }
}

/** Read-only render of a settled, answered group: each question's header + text + the chosen answer. */
@Composable
private fun AnsweredQuestionGroup(questions: List<UiQuestion>, resolution: QuestionResolution.Answered) {
    questions.forEachIndexed { index, question ->
        if (index > 0) Spacer(modifier = Modifier.height(AuraSpacing.Composer.internalPadding))
        HeaderChip(header = question.header)
        Spacer(modifier = Modifier.height(AuraSpacing.Composer.gapTight))
        Text(text = question.question, style = AuraType.bodyMessage, color = AuraColors.textPrimary)
        Spacer(modifier = Modifier.height(AuraSpacing.Composer.gapTight))
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(AuraSpacing.Composer.gapTight),
        ) {
            Icon(
                imageVector = Icons.Filled.CheckCircle,
                contentDescription = null,
                tint = AuraColors.accentPrimary,
                modifier = Modifier.size(AuraSpacing.Composer.iconSize),
            )
            Text(
                text = resolution.answers.getOrNull(index).renderAnswer(question),
                style = AuraType.bodyMessage,
                color = AuraColors.textPrimary,
            )
        }
    }
}

/** The chosen answer as display text: free text verbatim, else the selected option labels joined. */
private fun UiAnswer?.renderAnswer(question: UiQuestion): String {
    if (this == null) return "—"
    text?.let { return it }
    val indexes = selectedIndexes ?: return "—"
    return indexes.mapNotNull { question.options.getOrNull(it)?.label }.joinToString(", ").ifBlank { "—" }
}

/** One question's mutable answer state in the pending card. Options and free text are mutually
 * exclusive (the wire is `selected_indexes` XOR `text`), enforced by [selectOption]/[typeOther]. */
private class QuestionChoice {
    val indexes: SnapshotStateList<Int> = mutableStateListOf()
    var otherText: String by mutableStateOf("")
    var otherActive: Boolean by mutableStateOf(false)

    /** Tap an option: switch off any free-text answer, then set (single) or toggle (multi) the index. */
    fun selectOption(index: Int, multiSelect: Boolean) {
        otherActive = false
        if (multiSelect) {
            if (index in indexes) indexes.remove(index) else indexes.add(index)
        } else {
            indexes.clear()
            indexes.add(index)
        }
    }

    /** Type into "Other": a non-blank value becomes the answer and clears any option selection. */
    fun typeOther(value: String) {
        otherText = value
        otherActive = value.isNotBlank()
        if (value.isNotBlank()) indexes.clear()
    }

    /** The wire answer for this question, or `null` when it isn't validly answered yet (so Submit
     * stays disabled). Free text wins when active; otherwise the selection must satisfy the arity. */
    fun answerItemOrNull(question: UiQuestion): QuestionAnswerItemDto? {
        if (otherActive && otherText.isNotBlank()) return QuestionAnswerItemDto(text = otherText.trim())
        if (question.options.isEmpty() || indexes.isEmpty()) return null
        if (!question.multiSelect && indexes.size != 1) return null
        return QuestionAnswerItemDto(selectedIndexes = indexes.sorted())
    }
}
