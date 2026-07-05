package com.mewbo.aura.ui.chat

import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.mewbo.aura.data.device.DeviceToolExecutor
import com.mewbo.aura.data.model.AttachmentPayload
import com.mewbo.aura.data.model.ChatItem
import com.mewbo.aura.data.model.SessionEvent
import com.mewbo.aura.data.model.TextPayload
import com.mewbo.aura.data.model.TranscriptReducer
import com.mewbo.aura.data.repo.AttachmentRepository
import com.mewbo.aura.data.repo.ModelRepository
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.repo.SendResult
import com.mewbo.aura.data.repo.SessionRepository
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
 * plus voice-turn speak-along via [SpeechController], Gitea #180 P3/P4) and composer dictation
 * ([Transcriber], Gitea #180 P2) are the two legal `voice/` dependencies this view model carries
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
    private val deviceToolExecutor: DeviceToolExecutor,
) : ViewModel() {

    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    private val binding = SessionBinding()
    private val speech = SpeechController(synthesizer, viewModelScope)
    private var streamJob: Job? = null
    private var dictationJob: Job? = null
    private var lastSentText: String? = null

    private var reducerState = TranscriptReducer.State()
    private var clientTail: List<ChatItem> = emptyList()

    /** The global "new chat" default (`SettingsStore.selectedModel`), tracked separately from
     * [ChatUiState.selectedModel] so [bind] can restore it verbatim when leaving a session whose
     * model was hydrated from its own transcript (review fix, W2) - the live collector below keeps
     * both in sync for the landing/no-session case; [bind] is the one place that intentionally
     * overrides [ChatUiState.selectedModel] away from this value. */
    private var globalModelPreference: String? = null

    /** The default-project setting (`SettingsStore.selectedProject`, Gitea #178 W1-A), tracked the
     * same way [globalModelPreference] is: [bind] reseeds [ChatUiState.composerScope]'s
     * `selectedProjectKey` from this on every fresh/new chat (via [reseedProjectForBind]), but
     * (unlike the model) [selectProject]'s per-chat override never writes back through to
     * [settingsStore] - it must keep winning until the NEXT new chat, not just until this flow's
     * next emission. */
    private var globalProjectPreference: String? = null

    /** True once [selectProject] overrode the current fresh chat's project - blocks the
     * settings-collector reseed below from clobbering an un-sent per-chat pick (the chat's
     * ViewModel survives a Settings round-trip; #178 review find). Reset by [bind]. */
    private var projectOverriddenForCurrentChat = false

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
     * [handoffModality] (Gitea #180 P1) is the assist-overlay handoff's raw `EXTRA_HANDOFF_MODALITY`
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
        // A dictation in progress belongs to the session being LEFT - a switch (including to/from
        // a fresh/null chat) must not leave it listening into whatever session binds next.
        dictationJob?.cancel()
        dictationJob = null
        // Same for any speech in flight (Gitea #180 P3 barge-in trigger: "bind() session switch") -
        // a voice turn's speak-along belongs to the session being left, never the one binding next.
        speech.bargeIn()
        // Every real session switch (including to/from a fresh/null chat) cuts device-tool
        // servicing for whatever session was previously bound - subscribeLive() below re-attaches
        // it once a run is actually confirmed live on the NEW session, so a tool call belonging to
        // a chat the user has navigated away from is never dispatched (task brief: device tool
        // servicing must track which session is actually open, not just which one last ran).
        deviceToolExecutor.detach()
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

        viewModelScope.launch {
            try {
                val history = sessionRepository.fetchHistory(id)
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
                        selectedModel = hydratedModel,
                        composerScope = it.composerScope.copy(
                            selectedProjectKey = SessionEvent.lastContextProject(history.events),
                            activeToolIds = SessionEvent.lastContextMcpTools(history.events),
                        ),
                    )
                }
                // Gitea #181 fix wave, finding 1: primes THIS (fresh) SpeechController with
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
                _state.update { it.copy(isLoadingHistory = false) }
                appendClientError(e.message ?: "Couldn't load this conversation")
            }
        }
    }

    /** [modality] (Gitea #180 P1, default [InputModality.Text]) tags the turn THIS send opens -
     * client-only, never reaches the backend (see [InputModality]'s KDoc). Callers besides
     * `AssistTurnMachine`'s handoff-modality threading (via [bind]) all rely on the default. */
    fun send(text: String, modality: InputModality = InputModality.Text) {
        val trimmed = text.trim()
        if (trimmed.isBlank()) return
        lastSentText = trimmed

        // Barge-in (Gitea #180 P3: "new send()") - whatever was speaking (a P3 speak-along or a P4
        // manual read-aloud) never survives into the next turn. A voice-initiated send additionally
        // resets the per-conversation mute - fresh turn, fresh consent to speak (#175 §7.1).
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
            } catch (e: Exception) {
                _state.update { it.copy(isUploadingAttachments = false) }
                if (isSteering) {
                    // The optimistic bubble's pending=true echo (above) is only ever cleared by a
                    // REAL server user/user_steer event dedupe-matching it
                    // (TranscriptReducer.foldUserText) - a send that never reached the backend at
                    // all will never produce one, so the bubble was stuck at 70% opacity forever
                    // (fix-round-3 minor #12). Re-folding the same text (default pending=false)
                    // makes foldUserText treat this as that echo and clear it - the exact
                    // mechanism a real success would have used, no new reducer path needed.
                    applyEvent(SessionEvent.User(ts = Instant.now().toString(), payload = TextPayload(trimmed)))
                }
                appendClientError(e.message ?: "Couldn't send that message")
            }
        }
    }

    /** Client-side detach only - the backend run keeps going (v1 semantics, task brief). Also a
     * barge-in trigger (Gitea #180 P3: "the run-stop control") - stopping the run stops its speech. */
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
     * through [SpeechController.speakFinalized] (Gitea #180 P4) so read-aloud shares the exact same
     * markdown-stripping/sentence-chunking as speak-along, instead of speaking the raw string -
     * [SentenceChunker.stripMarkdown]'s own contract test asserts the exact fidelity this buys
     * (code fences -> "Code block omitted.", links -> link text, emphasis stripped). */
    fun toggleReadAloud(item: ChatItem.AssistantMessage) {
        val alreadySpeaking = _state.value.speakingKey == item.key
        speech.bargeIn()
        if (!alreadySpeaking) speech.speakFinalized(item.key, item.text)
    }

    /**
     * The sticky stop control's tap handler (Gitea #180 P4 two-widget pattern) - stops WHATEVER is
     * currently speaking (a P3 speak-along or a P4 manual read-aloud), identically to
     * [toggleReadAloud]'s own stop branch, but additionally latches the per-conversation mute
     * (#175 §7.1) when it interrupts THIS turn's own live voice-modality speech
     * ([SendDecision.shouldMuteOnStopSpeaking] has the "why not always" reasoning). [send]'s next
     * voice-initiated call is what resets [ChatUiState.speechMuted] back.
     */
    fun stopSpeaking() {
        val mute = SendDecision.shouldMuteOnStopSpeaking(_state.value.activeTurnModality, _state.value.runPhase)
        speech.bargeIn()
        if (mute) _state.update { it.copy(speechMuted = true) }
    }

    /**
     * Composer mic tap (Gitea #180 P2) - the permission request itself lives at the UI layer
     * (`ChatScreen`); this is only ever called once RECORD_AUDIO is actually granted. A no-op while
     * already listening (defensive - [ComposerState][com.mewbo.aura.ui.composer.ComposerState]'s
     * own C1/C3 split means the mic glyph and the stop tile are never both reachable at once).
     */
    fun startDictation() {
        if (_state.value.dictation is DictationState.Listening) return
        // Barge-in (Gitea #180 P3: "startDictation()") - about to speak into the mic, so whatever
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
        // Both the chat transcript collector below and the device-tool executor subscribe to this
        // SAME Flow instance - RunRepository.live() multicasts (shareIn) per session id, so the
        // two collectors share one underlying SSE connection instead of each opening their own
        // (data/CLAUDE.md "don't fork chat rendering" - chat rendering itself, applyEvent/
        // TranscriptReducer below, is untouched by the executor).
        val events = runRepository.live(id)
        deviceToolExecutor.attach(id, events)
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
    // Gitea #181 fix wave, finding 1: speakAlong defaults true (byte-equivalent for every existing
    // call site) - bind()'s own history-replay loop is the ONE caller that passes false, so
    // catching up a fresh binding's reducer state never routes already-finalized-elsewhere history
    // through the live speak-along pipeline. See bind()'s own primeAlreadySpoken call, right after
    // that replay loop, for the other half of this fix.
    private fun applyEvent(event: SessionEvent, pending: Boolean = false, completionPhase: RunPhase? = null, speakAlong: Boolean = true) {
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
        // Gitea #180 P3: feeds the transcript's current LAST assistant message through the speak-
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
