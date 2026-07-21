package com.mewbo.aura.ui.chat

import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.api.QuestionAnswerItemDto
import com.mewbo.aura.data.model.AttachmentPayload
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TextPayload
import com.mewbo.aura.data.model.TranscriptReducer
import com.mewbo.aura.data.repo.AttachmentRepository
import com.mewbo.aura.data.repo.ModelRepository
import com.mewbo.aura.data.repo.QuestionAnswerResult
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.repo.SendResult
import com.mewbo.aura.data.repo.SessionRepository
import com.mewbo.aura.data.repo.SessionTerminatedException
import com.mewbo.aura.data.repo.SessionScopeRepository
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.ui.composer.StagedAttachmentsReducer
import com.mewbo.aura.voice.AuraHaptics
import com.mewbo.aura.voice.InputModality
import com.mewbo.aura.voice.SentenceChunker
import com.mewbo.aura.voice.SpeechController
import com.mewbo.aura.voice.Synthesizer
import com.mewbo.aura.voice.Transcriber
import com.mewbo.aura.voice.TranscriberError
import com.mewbo.aura.voice.TranscriberEvent
import dagger.hilt.android.lifecycle.HiltViewModel
import java.time.Instant
import javax.inject.Inject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.transformWhile
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * One session's chat state. Owns no transport - everything routes through [SessionRepository] /
 * [RunRepository], which own the REST + SSE mechanics (data/CLAUDE.md). [Synthesizer] (read-aloud,
 * plus voice-turn speak-along via [SpeechController]) and composer dictation
 * ([Transcriber]) are the two legal `voice/` dependencies this view model carries
 * (viewmodels -> voice is legal layering, apps/mewbo_aura/CLAUDE.md) - it owns no other audio/
 * speech behavior.
 *
 * [items][ChatUiState.items] is always [TranscriptReducer]'s output over an internally-accumulated
 * [TranscriptReducer.State], folded incrementally via [TranscriptReducer.fold] - never
 * hand-assembled. The local optimistic user bubble in [send] is pushed through that SAME fold path
 * as a synthetic [SessionEvent.User]; the reducer's own echo-window dedupe (data/CLAUDE.md
 * "Optimistic local echo ... deduped by text + ts-window identity") is what collapses it with the
 * server's real `user`/`user_steer` event once it arrives - no separate dedupe logic here.
 */
@HiltViewModel
class ChatViewModel @Inject constructor(
    private val sessionRepository: SessionRepository,
    private val runRepository: RunRepository,
    private val synthesizer: Synthesizer,
    private val transcriber: Transcriber,
    private val haptics: AuraHaptics,
    private val modelRepository: ModelRepository,
    private val settingsStore: SettingsStore,
    private val sessionScopeRepository: SessionScopeRepository,
    private val attachmentRepository: AttachmentRepository,
) : ViewModel() {

    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    private val binding = SessionBinding()
    private val speech = SpeechController(synthesizer, viewModelScope)
    private var streamJob: Job? = null
    private var dictationJob: Job? = null

    /**
     * The in-flight session LOAD - [bind]'s history fetch, or [retryFromMessage]'s rewind (a POST
     * plus that same fetch). Tracked exactly like [streamJob] and [dictationJob], and cancelled by
     * [bind] for exactly the same reason: it belongs to the session being LEFT, and everything it is
     * about to write (the reducer, `items`, `title`, `sessionEnded`, the model/scope hydration, the
     * live subscription) is state SHARED with whatever session binds next.
     *
     * Untracked, a load that outlived a session switch folded session A's transcript into session
     * B's binding and pointed [streamJob] at A's run. [retryFromMessage] made that window much wider
     * (a network POST *then* a fetch, not just a fetch), but it was already open in [bind] itself -
     * so the fix lives here, on the one field both paths route through, rather than on the retry
     * path alone.
     */
    private var historyJob: Job? = null
    private var lastSentText: String? = null

    private var reducerState = TranscriptReducer.State()
    private var clientTail: List<ChatItem> = emptyList()

    /** The global "new chat" default (`SettingsStore.selectedModel`), tracked separately from
     * [ChatUiState.selectedModel] so [bind] can restore it verbatim when leaving a session whose
     * model was hydrated from its own transcript (review fix, W2) - the live collector below keeps
     * both in sync for the landing/no-session case; [bind] is the one place that intentionally
     * overrides [ChatUiState.selectedModel] away from this value. */
    private var globalModelPreference: String? = null

    /** The default-project setting (`SettingsStore.selectedProject`), tracked the
     * same way [globalModelPreference] is: [bind] reseeds [ChatUiState.composerScope]'s
     * `selectedProjectKey` from this on every fresh/new chat (via [reseedProjectForBind]), but
     * (unlike the model) [selectProject]'s per-chat override never writes back through to
     * [settingsStore] - it must keep winning until the NEXT new chat, not just until this flow's
     * next emission. */
    private var globalProjectPreference: String? = null

    /** True once [selectProject] overrode the current fresh chat's project - blocks the
     * settings-collector reseed below from clobbering an un-sent per-chat pick (the chat's
     * ViewModel survives a Settings round-trip). Reset by [bind]. */
    private var projectOverriddenForCurrentChat = false

    /** `SettingsStore.streamlitWidgetsEnabled` mirror, the replay-gate input for
     * [applyEvent]. Defaults to the store's own default (ON) until the collector below emits, so a
     * first-frame history replay never renders a widget the flag would forbid. */
    private var widgetsEnabled: Boolean = true

    init {
        viewModelScope.launch {
            settingsStore.baseUrl.collect { url -> _state.update { it.copy(isOffline = url.isBlank()) } }
        }
        viewModelScope.launch {
            settingsStore.displayName.collect { name -> _state.update { it.copy(displayName = name) } }
        }
        viewModelScope.launch {
            settingsStore.selectedModel.collect { model ->
                globalModelPreference = model.ifBlank { null }
                _state.update { it.copy(selectedModel = globalModelPreference) }
            }
        }
        viewModelScope.launch {
            settingsStore.streamlitWidgetsEnabled.collect { widgetsEnabled = it }
        }
        viewModelScope.launch {
            // Only seeds the CURRENT scope while still on a fresh/unsaved chat (binding.currentId ==
            // null) - this flow only re-emits when the Settings screen writes a new default, and an
            // already-open session's frozen scope must not be clobbered by that (bind()'s own
            // reseedProjectForBind call covers the "new chat" reseed path below).
            settingsStore.selectedProject.collect { project ->
                globalProjectPreference = project.ifBlank { null }
                if (binding.currentId == null && !projectOverriddenForCurrentChat) {
                    _state.update { it.copy(composerScope = it.composerScope.copy(selectedProjectKey = globalProjectPreference)) }
                }
            }
        }
        viewModelScope.launch {
            speech.speakingKey.collect { key -> _state.update { it.copy(speakingKey = key) } }
        }
        // Eager so the title bar shows the effective model ("Sonnet 4.6", not the "Core"
        // fallback) from first paint; startup failures stay silent - the sheet's own open
        // retries with a visible notice.
        loadModelsIfNeeded()
    }

    /** Loads the model catalog once (app start + retried on each picker-sheet open). A
     * failed/offline fetch calls [onNotice] and leaves [ChatUiState.models] untouched (never
     * crashes); a later call once a catalog IS cached is a no-op. */
    fun loadModelsIfNeeded(onNotice: (String) -> Unit = {}) {
        if (_state.value.models != null) return
        viewModelScope.launch {
            val catalog = modelRepository.catalog()
            if (catalog != null) {
                _state.update { it.copy(models = catalog) }
            } else {
                onNotice("Couldn't load models")
            }
        }
    }

    /** Persists the picker's selection ([SettingsStore]) and reflects it immediately in state - the
     * next [send] that starts a fresh session carries it as `context.model`. */
    fun selectModel(id: String?) {
        _state.update { it.copy(selectedModel = id) }
        viewModelScope.launch { settingsStore.setSelectedModel(id ?: "") }
    }

    /** Fetches both composer-options catalogs on every sheet open (task brief: no caching) -
     * failures degrade to [onNotice] rather than wiping whatever catalog/selection is already in
     * state. Existing selections ([ChatUiState.composerScope]'s `selectedProjectKey`/`activeToolIds`)
     * are preserved across a re-fetch. Tools are fetched scoped to the currently-selected project
     * (bug fix: this used to always fetch the unscoped/global catalog, so the picker never reflected
     * a project's own tool set) - [selectProject] is what keeps this scoped as the pick changes. */
    fun refreshComposerScope(onNotice: (String) -> Unit) {
        viewModelScope.launch {
            val project = _state.value.composerScope.selectedProjectKey
            val projectsDeferred = async { sessionScopeRepository.projects() }
            val toolsDeferred = async { sessionScopeRepository.tools(project = project) }
            val projects = projectsDeferred.await()
            val tools = toolsDeferred.await()
            if (projects == null && tools == null) {
                onNotice("Couldn't load project/tool options")
                return@launch
            }
            _state.update {
                it.copy(
                    composerScope = it.composerScope.copy(
                        projects = projects ?: it.composerScope.projects,
                        tools = tools ?: it.composerScope.tools,
                    ),
                )
            }
        }
    }

    /** Options sheet's Project pick - `null` = Temporary. Editable only pre-session (the sheet
     * itself gates the row's clickability once a session exists). A project switch resets
     * [ComposerScope.activeToolIds] to `null` (a narrowed set carried over from the PREVIOUS
     * project's tool catalog is meaningless against this one) and silently re-fetches the tools
     * catalog scoped to the new key - a failed re-fetch just leaves the prior catalog in place
     * rather than surfacing a notice, same degrade posture as [refreshComposerScope]. */
    fun selectProject(key: String?) {
        projectOverriddenForCurrentChat = true
        _state.update {
            it.copy(composerScope = it.composerScope.copy(selectedProjectKey = key, activeToolIds = null))
        }
        viewModelScope.launch {
            val tools = sessionScopeRepository.tools(project = key) ?: return@launch
            _state.update {
                // A fast double-switch can land this response after a NEWER pick — drop it then.
                if (it.composerScope.selectedProjectKey != key) it
                else it.copy(composerScope = it.composerScope.copy(tools = tools))
            }
        }
    }

    fun toggleTool(toolId: String) {
        _state.update { it.copy(composerScope = it.composerScope.toggleTool(toolId)) }
    }

    /** Toggle-sheet server-row bulk action - mirrors [toggleTool]'s style over
     * [ComposerScope.setServerTools]. */
    fun toggleServer(toolIds: List<String>, active: Boolean) {
        _state.update { it.copy(composerScope = it.composerScope.setServerTools(toolIds, active)) }
    }

    /** Resolves each picked [Uri] (display name/size/mime) and stages the ones under
     * [com.mewbo.aura.data.model.StagedAttachment.MAX_BYTES]; anything over that is dropped with an
     * [onNotice]. */
    fun stageAttachments(uris: List<Uri>, onNotice: (String) -> Unit) {
        if (uris.isEmpty()) return
        viewModelScope.launch {
            val resolved = uris.map { attachmentRepository.resolve(it) }
            val (next, oversized) = StagedAttachmentsReducer.add(_state.value.stagedAttachments, resolved)
            if (oversized.isNotEmpty()) {
                val label = if (oversized.size == 1) oversized.first().displayName else "${oversized.size} files"
                onNotice("$label over 25MB - skipped")
            }
            _state.update { it.copy(stagedAttachments = next) }
        }
    }

    fun removeStagedAttachment(uri: Uri) {
        _state.update { it.copy(stagedAttachments = StagedAttachmentsReducer.remove(it.stagedAttachments, uri)) }
    }

    /**
     * [ChatScreen] passes the nav-resolved `sessionId` here from a `LaunchedEffect(sessionId)`
     * rather than through constructor injection - a Hilt view model can't receive that
     * composable-scoped value without assisted-injection ceremony this app doesn't otherwise need.
     *
     * The rebind-vs-no-op decision itself lives in [SessionBinding] (task integ bug A + its
     * regression test, [SessionBindingTest]) - a redundant re-invocation with the same id
     * short-circuits, but any real switch (including to/from `null`) resets every piece of
     * per-session state before (re)binding.
     *
     * [ChatUiState.selectedModel] is reset to [globalModelPreference] here too (review fix, W2):
     * a fresh/new chat (`id == null`) always starts from the global "new chat" default, and an
     * EXISTING session's model is hydrated from its own transcript below - so switching between
     * chats never leaks one session's picked model into another's header/next-send scope.
     *
     * [handoffModality] is the assist-overlay handoff's raw `EXTRA_HANDOFF_MODALITY`
     * string (`ChatScreen`'s own nav-resolved arg, itself baked into the route by `AuraNavHost` from
     * `MainActivity`'s intent extra) - `null` for every ordinary bind (drawer/search navigation, a
     * fresh "New chat"), non-null only when this session is being picked up mid-handoff, in which
     * case it's what the (already-running) turn this bind is about to [subscribeLive] onto was
     * actually tagged with. [InputModality.fromExtra] parses it, defaulting to
     * [InputModality.Text] for the ordinary/no-handoff case.
     */
    fun bind(id: String?, handoffModality: String? = null) {
        if (!binding.rebindTo(id)) return

        projectOverriddenForCurrentChat = false
        streamJob?.cancel()
        streamJob = null
        // An in-flight history/retry load belongs to the session being LEFT too - left running, it
        // resumes after this reset and folds the OLD session's transcript into the new binding (see
        // historyJob's own doc). Cancellation is cooperative, so loadHistoryAndFollow ALSO re-checks
        // binding.isCurrent after each suspension point - the two together are what close this.
        historyJob?.cancel()
        historyJob = null
        // A dictation in progress belongs to the session being LEFT - a switch (including to/from
        // a fresh/null chat) must not leave it listening into whatever session binds next.
        dictationJob?.cancel()
        dictationJob = null
        // Same for any speech in flight (barge-in trigger: "bind() session switch") -
        // a voice turn's speak-along belongs to the session being left, never the one binding next.
        speech.bargeIn()
        reducerState = TranscriptReducer.State()
        clientTail = emptyList()
        lastSentText = null
        _state.update {
            it.copy(
                items = emptyList(),
                title = null,
                runPhase = RunPhase.Idle,
                speakingKey = null,
                isLoadingHistory = id != null,
                sessionId = id,
                // Reset the terminal state on every rebind - a switch away from a terminated session
                // must re-enable the composer; the history load below re-derives it.
                sessionEnded = false,
                selectedModel = globalModelPreference,
                composerScope = reseedProjectForBind(id, it.composerScope, globalProjectPreference),
                activeTurnModality = InputModality.fromExtra(handoffModality),
                dictation = DictationState.Idle,
                speechMuted = false,
            )
        }
        if (id == null) {
            // Silent prefetch so the pre-session scope indicator (ChatSurface) can show a tool
            // count on first paint, not just the project name - failures degrade to "no count yet"
            // (ComposerScope.activeToolCount stays null), never a toast for a background fetch the
            // user didn't ask for.
            refreshComposerScope(onNotice = {})
            return
        }

        historyJob = viewModelScope.launch { loadHistoryAndFollow(id) }
    }

    /**
     * Fetches [id]'s authoritative transcript, folds it into the (already-reset) reducer, hydrates
     * the session's own persisted scope, and attaches to its live stream if a run is in flight.
     *
     * Shared verbatim by the two callers that need a transcript rebuilt from the SERVER: [bind]
     * (a session switch) and [retryFromMessage] (a destructive rewind, where the client is holding
     * events the server has just deleted). Both must reach the identical end state, so this is ONE
     * body rather than two that drift.
     *
     * **Every write below is gated on [id] still being the bound session** ([SessionBinding.isCurrent]).
     * The caller's [historyJob] cancellation already covers most of the window, but cancellation only
     * lands at a suspension point, and the fetch is the last one before a long non-suspending run of
     * state mutation - so the explicit re-check is what makes "this load may write" a decision rather
     * than an assumption. Nothing in here is worth writing into a session the user has already left.
     */
    private suspend fun loadHistoryAndFollow(id: String) {
        try {
            val history = sessionRepository.fetchHistory(id)
            // The user opened a different session while this was in flight - drop it entirely rather
            // than fold session `id`'s transcript/title/scope into whatever is bound now.
            if (!binding.isCurrent(id)) return
            // speakAlong=false: catching up this fresh binding's reducer state from persisted
            // history is not a live turn - AssistUiState.Streaming's own SpeechController (the
            // assist-overlay handoff case, voice/CLAUDE.md's cross-instance handoff law) may
            // already have spoken some or all of this. primeAlreadySpoken below (after the
            // replay + hydration settle) is what marks it consumed instead, so the SAME
            // information reaches speech state without re-speaking it.
            history.events.forEach { applyEvent(it, speakAlong = false) }
            // Hydrate from the session's OWN last-persisted context (backend.py
            // `_load_last_context` semantics) so the frozen scope reflects what the NEXT turn
            // would actually run on, not the global new-chat default it was seeded with at bind.
            // model falls back to the global default when the session has no context/model;
            // project + tool-narrowing round-trip through the SAME persisted `context` event
            // (backend `_build_context_payload` copies the context object verbatim) and take NO
            // such fallback - absent project => Temporary (null), absent mcp_tools => all tools
            // (null), each an honest match of what the backend re-resolves. Without this a
            // revisited project-scoped session silently reverted to Temporary and, because the
            // backend re-resolves scope from EVERY /query's own context (nothing is sticky
            // server-side - SessionContext.kt), the next send MIGRATED the session onto the
            // wrong project/cwd.
            val hydratedModel = SessionEvent.lastContextModel(history.events) ?: globalModelPreference
            _state.update {
                it.copy(
                    title = history.title,
                    isLoadingHistory = false,
                    // Opening a permanently terminated session lands directly in its terminal
                    // state - composer disabled, no Retry - so the first send never has to 410
                    // to discover it (the events endpoint carries the authoritative flag).
                    sessionEnded = history.terminated,
                    selectedModel = hydratedModel,
                    composerScope = it.composerScope.copy(
                        selectedProjectKey = SessionEvent.lastContextProject(history.events),
                        activeToolIds = SessionEvent.lastContextMcpTools(history.events),
                    ),
                )
            }
            // primes THIS (fresh) SpeechController with
            // whatever text the replay above just folded in, so a voice-tagged binding (only
            // real case this isn't a no-op - see primeAlreadySpoken's own gating) never re-
            // speaks a first turn the overlay's OWN SpeechController instance already spoke
            // before handing off - a still-streaming last message (a mid-turn Expand tap) stays
            // primed-but-open, so subscribeLive's later live deltas for that same message still
            // speak only the genuinely new remainder.
            speech.primeAlreadySpoken(
                item = _state.value.items.lastOrNull { it is ChatItem.AssistantMessage } as? ChatItem.AssistantMessage,
                modality = _state.value.activeTurnModality,
            )
            if (history.running) subscribeLive(id)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Same gate on the failure path: session `id`'s load failing says nothing about the
            // session now bound, and an ErrorCard/spinner-clear written into IT would be a lie.
            if (!binding.isCurrent(id)) return
            _state.update { it.copy(isLoadingHistory = false) }
            appendClientError(e.message ?: "Couldn't load this conversation")
        }
    }

    /** [modality] (default [InputModality.Text]) tags the turn THIS send opens -
     * client-only, never reaches the backend (see [InputModality]'s KDoc). Callers besides
     * `AssistTurnMachine`'s handoff-modality threading (via [bind]) all rely on the default. */
    fun send(text: String, modality: InputModality = InputModality.Text) {
        val trimmed = text.trim()
        if (trimmed.isBlank()) return
        lastSentText = trimmed

        // Barge-in ("new send()") - whatever was speaking (a P3 speak-along or a P4
        // manual read-aloud) never survives into the next turn. A voice-initiated send additionally
        // resets the per-conversation mute - fresh turn, fresh consent to speak (§7.1).
        speech.bargeIn()
        if (modality == InputModality.Voice) _state.update { it.copy(speechMuted = false) }

        // A send while a run is already Sending/Streaming is a steer into that run (202 enqueue,
        // spec §6.2 C4) - known structurally from the CURRENT phase, no need to wait on the
        // network round-trip to know the bubble should render queued (spec §6.12). See
        // SendDecision's own doc for the bug this phaseForSend call fixes (fix-round-3 Important #3).
        val currentPhase = _state.value.runPhase
        val isSteering = SendDecision.isSteering(currentPhase)
        // Attachments can't ride a steer - `/message` has no attachments field (RunRepository) -
        // only a fresh-turn `/query` carries them.
        val attachmentsToSend = if (isSteering) emptyList() else _state.value.stagedAttachments

        // Optimistic echo threads the SAME staged-attachment metadata the upload call below is
        // about to send, so the tile row above the bubble shows instantly rather than waiting on
        // the server's own persisted echo (ChatItem.AttachmentSummary's KDoc).
        val optimisticAttachments = attachmentsToSend.map {
            AttachmentPayload(filename = it.displayName, contentType = it.mimeType, sizeBytes = it.sizeBytes)
        }.ifEmpty { null }
        applyEvent(
            SessionEvent.User(ts = Instant.now().toString(), payload = TextPayload(trimmed, attachments = optimisticAttachments)),
            pending = isSteering,
        )
        _state.update {
            it.copy(
                runPhase = SendDecision.phaseForSend(currentPhase, hasActiveStream = streamJob?.isActive == true),
                isUploadingAttachments = attachmentsToSend.isNotEmpty(),
                stagedAttachments = StagedAttachmentsReducer.clearOnSend(it.stagedAttachments, isSteering),
                activeTurnModality = SendDecision.modalityForSend(it.activeTurnModality, modality, isSteering),
            )
        }

        viewModelScope.launch {
            try {
                val id = binding.currentId ?: sessionRepository.createSession(
                    model = _state.value.selectedModel,
                    project = _state.value.composerScope.selectedProjectKey,
                    mcpTools = _state.value.composerScope.mcpToolsForContext(),
                ).also { binding.currentId = it }

                if (isSteering) {
                    when (runRepository.send(id, trimmed)) {
                        is SendResult.RunStarted -> subscribeLive(id)
                        is SendResult.Enqueued -> if (streamJob?.isActive != true) subscribeLive(id)
                        is SendResult.SlashHandled -> Unit
                    }
                } else {
                    val records = if (attachmentsToSend.isNotEmpty()) {
                        attachmentRepository.upload(id, attachmentsToSend, _state.value.selectedModel)
                    } else {
                        emptyList()
                    }
                    _state.update { it.copy(isUploadingAttachments = false) }
                    when (
                        runRepository.sendQuery(
                            sessionId = id,
                            text = trimmed,
                            model = _state.value.selectedModel,
                            project = _state.value.composerScope.selectedProjectKey,
                            mcpTools = _state.value.composerScope.mcpToolsForContext(),
                            attachments = records,
                        )
                    ) {
                        is SendResult.RunStarted -> subscribeLive(id)
                        is SendResult.Enqueued -> if (streamJob?.isActive != true) subscribeLive(id)
                        // /query's 200: a slash command was handled inline - no run started.
                        is SendResult.SlashHandled -> _state.update { it.copy(runPhase = RunPhase.Idle) }
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: SessionTerminatedException) {
                markSessionTerminated()
                if (isSteering) clearOptimisticSteerEcho(trimmed)
            } catch (e: Exception) {
                _state.update { it.copy(isUploadingAttachments = false) }
                if (isSteering) clearOptimisticSteerEcho(trimmed)
                appendClientError(e.message ?: "Couldn't send that message")
            }
        }
    }

    /**
     * The session is permanently terminated (a 410 on any mutation). Flips the pre-wired
     * terminal state: the composer disables and any ErrorCard drops its Retry
     * ([ChatUiState.sessionEnded]), instead of surfacing a retryable error that would 410 forever
     * (bug #3, the infinite-retry loop). runPhase must leave Sending/Streaming so the liveness spark
     * stops. Deliberately NO client ErrorCard - the calm terminated composer block carries the
     * message (DESIGN.md §6: no error residue).
     *
     * Shared by every mutation that can meet a 410: [send] and [retryFromMessage]. (A fork can too,
     * but [SessionRepository.forkSession] degrades to `null` rather than raising - and the gate
     * ([MessageAction.availableFor]) has already withdrawn all three actions by then anyway.)
     */
    private fun markSessionTerminated() {
        _state.update { it.copy(isUploadingAttachments = false, sessionEnded = true, runPhase = RunPhase.Done) }
    }

    /**
     * "Retry from here" ([MessageAction.RetryFromHere]) - re-runs [message] in the SAME session.
     * DESTRUCTIVE by design: `POST /recover {action:"retry", from_ts}` deletes that turn and
     * everything after it server-side, then the orchestrator re-appends the user event and runs a
     * fresh attempt (`SessionRuntime.resolve_recovery_query` - "retry = time-travel"). Earlier turns
     * survive.
     *
     * **Which is exactly why the client cannot just keep streaming.** [TranscriptReducer] is
     * additive - `fold` upserts, it has no delete - so the reducer state still holds every event the
     * server just destroyed, and no amount of replay removes them. Attaching to the new run would
     * render the retried turn UNDERNEATH the orphaned corpse of the old one.
     *
     * So the reducer is torn down to zero and rebuilt from the SERVER's transcript
     * ([resetTranscriptForReload] + [loadHistoryAndFollow], the same path [bind] uses). Rebuilding
     * from `GET /events` rather than leaning on the SSE backlog is deliberate: `/stream` does replay
     * the full persisted transcript on a FRESH connection, but `RunRepository.live()` hands out a
     * per-session multicast `SharedFlow` with `replay = 0`, so a subscriber that joins an
     * already-open one receives no backlog at all. The REST fetch is authoritative regardless of
     * which of those two happens, and the stream's own replay then folds in idempotently on top
     * (content-key dedupe) - so there is no gap between the fetch and the subscribe either.
     */
    fun retryFromMessage(message: ChatItem.UserBubble, onResult: (Boolean) -> Unit) {
        val id = binding.currentId
        if (id == null) {
            onResult(false)
            return
        }
        // Tracked as the session load it is (see [historyJob]) - it rebuilds this session's whole
        // transcript, so a session switch must be able to cancel it exactly like bind()'s own load.
        historyJob?.cancel()
        historyJob = viewModelScope.launch {
            try {
                // 202 or bust - a non-2xx raises (RunRepository.errorFor). The SendResult itself
                // carries nothing to branch on here: /recover has exactly one success shape.
                runRepository.retryFrom(id, message.ts, _state.value.selectedModel)
                // The rewind HAPPENED - report that truthfully and let the sheet close, before the
                // reload below (which shows its own spinner) and regardless of where the user has
                // navigated to since.
                onResult(true)
                // ...but only session `id` gets its transcript rebuilt. If the user opened a
                // different one while the POST was in flight, the rewind still stands server-side
                // and re-opening this session will show it; what must NOT happen is folding it into
                // whatever is bound now.
                if (!binding.isCurrent(id)) return@launch
                resetTranscriptForReload()
                loadHistoryAndFollow(id)
            } catch (e: CancellationException) {
                throw e
            } catch (e: SessionTerminatedException) {
                if (binding.isCurrent(id)) markSessionTerminated()
                onResult(false)
            } catch (e: Exception) {
                onResult(false)
            }
        }
    }

    /**
     * "Branch in new chat" ([MessageAction.BranchInNewChat], [fromTs] = the bubble's ts) and "Fork
     * session" ([MessageAction.ForkSession], [fromTs] = `null`) - the SAME `POST /fork`, differing
     * only in whether a cut point is sent. Non-destructive: THIS session is untouched, so there is
     * nothing to reset here; the new session id goes to [onResult] and the HOST navigates to it
     * (which rebinds this view model through [bind] like any other session switch).
     *
     * NOT tracked as a [historyJob] (unlike [retryFromMessage]) precisely BECAUSE it mutates nothing
     * here: cancelling it on a session switch would only abandon a fork the user asked for and the
     * server may already have made. Instead the RESULT is gated - if they have moved on, the fork
     * still exists (and appears in the drawer via the repository's own `refreshSessions`), we simply
     * don't yank them into it.
     *
     * The currently-selected model rides along so a fork made after re-picking the model in the top
     * bar actually runs on it - absent that the fork silently inherits the source's persisted one.
     * **Caveat:** on a session with NO persisted `context.model`, [bind] hydrates `selectedModel`
     * from [globalModelPreference], so the fork gets that model explicitly PINNED even though the
     * source session never had one pinned. Harmless (it is the model the user is looking at in the
     * top bar, and the one the next turn here would have used) but it is not a pure copy.
     *
     * **Asymmetry with [retryFromMessage], deliberately left in place:** `SessionRepository.forkSession`
     * degrades 409 / 410 / transport failure to a single `null` (that file's house idiom, matching
     * `renameSession`/`archiveSession`), so a session terminated by ANOTHER client cannot flip
     * [ChatUiState.sessionEnded] from here the way a retry's typed [SessionTerminatedException] does -
     * the fork just reports failure and the gate keeps offering actions until something else reveals
     * the termination. Surfacing it would mean either a typed exception in a repository built on
     * degrade-to-null, or a second result type; neither is worth it for a race this narrow.
     */
    fun forkSession(fromTs: String?, onResult: (String?) -> Unit) {
        val id = binding.currentId
        if (id == null) {
            onResult(null)
            return
        }
        viewModelScope.launch {
            val newSessionId = sessionRepository.forkSession(id, fromTs, _state.value.selectedModel)
            // Navigating is the caller's response to this. Don't drag the user out of a session they
            // deliberately opened while the fork was in flight - the fork itself is safe on the server.
            if (!binding.isCurrent(id)) return@launch
            onResult(newSessionId)
        }
    }

    /**
     * Tears the transcript down to nothing so the next [loadHistoryAndFollow] rebuilds it purely
     * from the server - the reducer included, since it is append-only and cannot un-see an event
     * (see [retryFromMessage]). Session-IDENTITY state ([bind]'s `sessionEnded`/`selectedModel`/
     * `composerScope`/modality resets) is deliberately NOT touched: this is the same session, only
     * its transcript changed.
     */
    private fun resetTranscriptForReload() {
        streamJob?.cancel()
        streamJob = null
        // Whatever was being spoken belongs to a turn that no longer exists (same barge-in trigger
        // family as bind()/stop()/send()).
        speech.bargeIn()
        reducerState = TranscriptReducer.State()
        clientTail = emptyList()
        _state.update {
            it.copy(items = emptyList(), runPhase = RunPhase.Idle, speakingKey = null, isLoadingHistory = true)
        }
    }

    /**
     * The optimistic bubble's `pending = true` echo ([send]) is only ever cleared by a REAL server
     * `user`/`user_steer` event dedupe-matching it ([TranscriptReducer.foldUserText]) - a send that
     * never reached the backend will never produce one, so the bubble would be stuck at 70% opacity
     * forever. Re-folding the same text (default `pending = false`) makes
     * `foldUserText` treat this as that echo and clear it - the exact mechanism a real success would
     * have used, no new reducer path needed. Shared by both send-failure catches ([send]).
     */
    private fun clearOptimisticSteerEcho(text: String) {
        applyEvent(SessionEvent.User(ts = Instant.now().toString(), payload = TextPayload(text)))
    }

    /**
     * Submits the human's [answers] to a pending ask-user question ([ChatItem.Question]) — the blocked
     * `ask_user_question` tool call resolves and the run continues. A HUMAN-tap answer wired the same
     * way retry/fork flow (ViewModel → repository), deliberately NOT the auto-serviced
     * `device_tool_call` pipeline.
     *
     * The AUTHORITATIVE card settle is the `user_question_answered` SSE event ([TranscriptReducer]),
     * which the live subscription is already following (the run is blocked, so `running` is true) — so
     * [onResult] only steers the card's local submitting state: `true` on accept/already-answered
     * (leave the spinner; the event flips the card read-only), `false` on genuine failure (the card
     * re-enables and shows a transient notice). No session id ⇒ `false` (nothing to answer against).
     */
    fun answerQuestion(callId: String, callToken: String, answers: List<QuestionAnswerItemDto>, onResult: (Boolean) -> Unit) {
        val id = binding.currentId
        if (id == null) {
            onResult(false)
            return
        }
        viewModelScope.launch {
            try {
                val result = runRepository.answerQuestion(id, callId, callToken, answers)
                onResult(result != QuestionAnswerResult.Failed)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                onResult(false)
            }
        }
    }

    /** Client-side detach only - the backend run keeps going (v1 semantics, task brief). Also a
     * barge-in trigger ("the run-stop control") - stopping the run stops its speech. */
    fun stop() {
        streamJob?.cancel()
        streamJob = null
        speech.bargeIn()
        _state.update {
            if (it.runPhase == RunPhase.Streaming || it.runPhase == RunPhase.Sending) it.copy(runPhase = RunPhase.Idle) else it
        }
    }

    fun retry() {
        lastSentText?.let(::send)
    }

    /** Spec §6.5: tap the action row's read-aloud glyph on a specific completed turn. Routed
     * through [SpeechController.speakFinalized] so read-aloud shares the exact same
     * markdown-stripping/sentence-chunking as speak-along, instead of speaking the raw string -
     * [SentenceChunker.stripMarkdown]'s own contract test asserts the exact fidelity this buys
     * (code fences -> "Code block omitted.", links -> link text, emphasis stripped). */
    fun toggleReadAloud(item: ChatItem.AssistantMessage) {
        val alreadySpeaking = _state.value.speakingKey == item.key
        speech.bargeIn()
        if (!alreadySpeaking) speech.speakFinalized(item.key, item.text)
    }

    /**
     * The sticky stop control's tap handler (two-widget pattern) - stops WHATEVER is
     * currently speaking (a P3 speak-along or a P4 manual read-aloud), identically to
     * [toggleReadAloud]'s own stop branch, but additionally latches the per-conversation mute
     * (§7.1) when it interrupts THIS turn's own live voice-modality speech
     * ([SendDecision.shouldMuteOnStopSpeaking] has the "why not always" reasoning). [send]'s next
     * voice-initiated call is what resets [ChatUiState.speechMuted] back.
     */
    fun stopSpeaking() {
        val mute = SendDecision.shouldMuteOnStopSpeaking(_state.value.activeTurnModality, _state.value.runPhase)
        speech.bargeIn()
        if (mute) _state.update { it.copy(speechMuted = true) }
    }

    /**
     * Composer mic tap - the permission request itself lives at the UI layer
     * (`ChatScreen`); this is only ever called once RECORD_AUDIO is actually granted. A no-op while
     * already listening (defensive - [ComposerState][com.mewbo.aura.ui.composer.ComposerState]'s
     * own C1/C3 split means the mic glyph and the stop tile are never both reachable at once).
     */
    fun startDictation() {
        if (_state.value.dictation is DictationState.Listening) return
        // Barge-in ("startDictation()") - about to speak into the mic, so whatever
        // was speaking stops.
        speech.bargeIn()
        dictationJob?.cancel()
        dictationJob = viewModelScope.launch {
            try {
                transcriber.listen().collect { event ->
                    if (event is TranscriberEvent.Final) haptics.transcriptAccepted()
                    if (event is TranscriberEvent.Error && event.code == TranscriberError.Unavailable) {
                        _state.update { it.copy(dictationAvailable = false) }
                    }
                    _state.update { it.copy(dictation = DictationDecision.next(it.dictation, event)) }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.update { it.copy(dictation = DictationState.Idle) }
            }
        }
    }

    /** Composer C3's stop tile - ends listening WITHOUT sending, keeping whatever partial transcript
     * had already landed (spec ruling) by routing it through the same [DictationState.Final] path a
     * real recognizer Final uses (see [DictationState]'s own KDoc) - no separate "stopped" case
     * needed. [haptics.transcriptAccepted] deliberately does NOT fire here - that's reserved for a
     * genuine recognizer [TranscriberEvent.Final] in [startDictation]. */
    fun stopDictation() {
        dictationJob?.cancel()
        dictationJob = null
        val partial = (_state.value.dictation as? DictationState.Listening)?.partial
        _state.update { it.copy(dictation = if (partial != null) DictationState.Final(partial) else DictationState.Idle) }
    }

    /** `ChatSurface`'s one-shot ack once it's applied a [DictationState.Final] to the draft/pending-
     * modality - resets to [DictationState.Idle] so the same text can't replay on a later
     * recomposition/rotation. A stale/already-superseded ack (dictation moved on before this landed)
     * is a no-op. */
    fun consumeDictationFinal() {
        _state.update { if (it.dictation is DictationState.Final) it.copy(dictation = DictationState.Idle) else it }
    }

    /** Pull-up handoff draft (user directive 2026-07-04): text carried from the assist overlay's
     * pill when it was swiped up into the app (`AuraNavHost` reads it off the chat destination's
     * `SavedStateHandle` and seeds it here). Deliberately its OWN one-shot field rather than a ride
     * on the [DictationState.Final] path: a Final also tags the pending send Voice, which would
     * trigger speak-along on a typed draft's reply. */
    fun seedHandoffDraft(text: String) {
        if (text.isBlank()) return
        _state.update { it.copy(pendingHandoffDraft = text) }
    }

    /** `ChatSurface`'s one-shot ack for [seedHandoffDraft] — same replay-safety shape as
     * [consumeDictationFinal]. */
    fun consumeHandoffDraft() {
        _state.update { if (it.pendingHandoffDraft != null) it.copy(pendingHandoffDraft = null) else it }
    }

    private fun subscribeLive(id: String) {
        streamJob?.cancel()
        _state.update { it.copy(runPhase = RunPhase.Streaming) }
        // Device-tool servicing is NOT wired here: RunRepository.live() attaches the executor to the
        // flow it returns (DeviceToolDispatch's KDoc has why that seam and not this one), and
        // multicasts (shareIn) per session id, so the transcript collector below and the executor
        // share ONE underlying SSE connection rather than each opening their own. Chat rendering
        // (applyEvent/TranscriptReducer) is untouched by any of that.
        val events = runRepository.live(id)
        streamJob = viewModelScope.launch {
            try {
                // A SharedFlow's own collect() never completes on its own (unlike the raw cold
                // flow this used to collect directly) - transformWhile is what makes THIS
                // collector actually stop once the run is genuinely over, instead of idling
                // forever. It emits every event through unchanged, then stops pulling further
                // ones the moment it sees stream_end - the definitive "nothing more is ever
                // coming through this connection" signal (SessionStreamClient's own terminal-frame
                // handling) - or a materialized StreamError (review finding F2's upstream-failure
                // path). Without this, the Done-fallback below and the catch block underneath were
                // both dead code (review finding F2).
                events
                    .transformWhile { event ->
                        emit(event)
                        event !is SessionEvent.StreamEnd && event !is SessionEvent.StreamError
                    }
                    .collect { event ->
                        if (event is SessionEvent.StreamError) {
                            appendClientError(event.message)
                        } else {
                            applyEvent(event, completionPhase = completionPhaseFor(event))
                        }
                    }
                // Reached once transformWhile above lets collect() return - the fallback for a
                // stream_end that arrived without a prior Completion event ever setting runPhase.
                _state.update { if (it.runPhase == RunPhase.Streaming) it.copy(runPhase = RunPhase.Done) else it }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                appendClientError(e.message ?: "Lost connection to the run")
            }
        }
    }

    /** `null` for every non-[SessionEvent.Completion] event - [applyEvent] leaves [ChatUiState.runPhase]
     * untouched in that case. */
    private fun completionPhaseFor(event: SessionEvent): RunPhase? {
        if (event !is SessionEvent.Completion) return null
        val failed = event.payload.error != null || event.payload.lastError != null
        return if (failed) RunPhase.Error else RunPhase.Done
    }

    /**
     * [completionPhase], when non-null, lands in the SAME [_state] emission as the folded [items]
     * update - never a separate later `_state.update`. Splitting them (the original shape here)
     * opens an observable intermediate state: a `Completion` event's fold already flips the
     * just-finalized [ChatItem.AssistantMessage.isStreaming] to `false` (so [ChatTranscript] renders
     * it as settled markdown) one emission BEFORE `runPhase` actually leaves [RunPhase.Streaming].
     * Since the 2026-07-04 liveness directive, [ChatTranscript]'s `showThinking = isRunLive` keeps
     * the thinking spark visible for the WHOLE run (`Sending`/`Streaming`) - so in that split gap
     * `runPhase` is still `Streaming` and the spark would flash on for one frame BELOW an already-
     * finalized reply, right at turn completion. Folding both into one `copy(...)` clears `runPhase`
     * in lockstep with the finalized items, making that inconsistent intermediate state unobservable
     * by construction.
     */
    // speakAlong defaults true (byte-equivalent for every existing
    // call site) - bind()'s own history-replay loop is the ONE caller that passes false, so
    // catching up a fresh binding's reducer state never routes already-finalized-elsewhere history
    // through the live speak-along pipeline. See bind()'s own primeAlreadySpoken call, right after
    // that replay loop, for the other half of this fix.
    private fun applyEvent(event: SessionEvent, pending: Boolean = false, completionPhase: RunPhase? = null, speakAlong: Boolean = true) {
        // replay gate: `widget_ready` events are server-persisted, so a history re-fold
        // (bind) or SSE backlog replay (subscribeLive) - both routed through here - would otherwise
        // re-render (and re-boot Pyodide for) a widget created earlier even AFTER the user turned the
        // Streamlit-widgets capability OFF. Drop it at this ONE ingestion seam so the flag gates
        // RENDER as well as advertise, while `TranscriptReducer` stays pure (no settings DI). A NEW
        // widget can't arrive while the flag is off anyway (the `stlite` header isn't sent), so this
        // only suppresses replay of previously-created ones. The assist overlay needs no equivalent:
        // it only ever folds `beginTurn`'s fresh brand-new session (no history replay), and any live
        // widget there still requires the flag-gated header - safe by construction (voice/CLAUDE.md).
        if (widgetGateDropsReplay(event, widgetsEnabled)) return
        reducerState = TranscriptReducer.fold(reducerState, event, pending)
        clientTail = emptyList()
        publish(completionPhase, speakAlong)
    }

    private fun appendClientError(message: String) {
        val ts = Instant.now().toString()
        clientTail = clientTail + ChatItem.ErrorCard(message = message, ts = ts, key = "client-error:$ts")
        publish(completionPhase = RunPhase.Error)
    }

    private fun publish(completionPhase: RunPhase? = null, speakAlong: Boolean = true) {
        val items = reducerState.chatItems + clientTail
        // feeds the transcript's current LAST assistant message through the speak-
        // along pipeline on every fold - SpeechController's own modality/mute/closedKey gating makes
        // this a no-op whenever there's nothing new to speak (e.g. an unrelated client-error tail).
        // speakAlong=false (bind()'s history replay only) skips this entirely instead of relying on
        // that gating alone - see applyEvent's own KDoc.
        if (speakAlong) {
            speech.onAssistantMessage(
                item = items.lastOrNull { it is ChatItem.AssistantMessage } as? ChatItem.AssistantMessage,
                modality = _state.value.activeTurnModality,
                muted = _state.value.speechMuted,
            )
        }
        _state.update { it.copy(items = items, runPhase = completionPhase ?: it.runPhase) }
    }
}

/**
 * The widget replay gate as a pure predicate (extracted top-level, like [SessionBinding], so
 * it's directly unit-testable without constructing the whole ViewModel + its Context-backed
 * `SettingsStore`): `true` iff [event] is a `widget_ready` that must be DROPPED because the user has
 * the Streamlit-widgets capability turned off. Every other event, and every event while
 * [widgetsEnabled] is on, passes (`false`). Composed with the real [TranscriptReducer] in
 * [ChatWidgetGateTest] to prove a suppressed widget never becomes a [ChatItem.Widget].
 */
internal fun widgetGateDropsReplay(event: SessionEvent, widgetsEnabled: Boolean): Boolean =
    event is SessionEvent.WidgetReady && !widgetsEnabled
