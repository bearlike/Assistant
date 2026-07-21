// DRY note: the turn-reconstruction + token-usage logic in this file
// (buildTimeline, computeTurnTokenUsage) is also ported to Python for the MCP
// server at apps/mewbo_mcp/src/mewbo_mcp/timeline.py. The two implementations
// MUST stay behaviorally in sync; a parity test
// (apps/mewbo_mcp/tests/test_timeline.py) checks shared fixtures. When you
// change turn-boundary or token-usage logic here, update the Python port too.
import { AttachmentPayload, CompactionMeta, DiffFile, EventRecord, SessionContext, TimelineEntry, TodoItem, TodoItemStatus, TodoMeta, TriggerTranscriptMeta, TurnMeta, TurnTokenUsage, UserQuestionAnsweredPayload, UserQuestionPayload, WidgetReadyPayload } from "../types";
import { extractUnifiedDiffs, mergeDiffFiles } from "./diff";
import { parseOutcomeAssertion, parseRunFailure, parseStructuredResult } from "./logs";
import { formatDuration } from "./time";
import { readNumber, readString } from "./payload";

function computeTurnTokenUsage(turnEvents: EventRecord[]): TurnTokenUsage | undefined {
  // PEAK for input (context pressure), SUM for output (additive).
  // Within a turn, input_tokens on each call grows as tool results stack
  // onto the same prompt — summing double-counts everything. The peak is
  // the real pressure on the model's context window.
  let peakRootInput = 0;
  let outputTokens = 0;
  let billedRootInput = 0;
  let billedSubInput = 0;
  const subPeakPerAgent = new Map<string, number>();
  let subOutputTokens = 0;
  const subAgents = new Set<string>();
  let cacheCreationTokens = 0;
  let cacheReadTokens = 0;
  let reasoningTokens = 0;
  for (const e of turnEvents) {
    if (e.type !== "llm_call_end") continue;
    const p = e.payload as Record<string, unknown>;
    const depth = readNumber(p, "depth") ?? 0;
    const inTok = readNumber(p, "input_tokens") ?? 0;
    const outTok = readNumber(p, "output_tokens") ?? 0;
    const cacheCreate = readNumber(p, "cache_creation_input_tokens") ?? 0;
    const cacheRead = readNumber(p, "cache_read_input_tokens") ?? 0;
    const reasoning = readNumber(p, "reasoning_output_tokens") ?? 0;
    cacheCreationTokens += cacheCreate;
    cacheReadTokens += cacheRead;
    reasoningTokens += reasoning;
    if (depth === 0) {
      if (inTok > peakRootInput) peakRootInput = inTok;
      outputTokens += outTok;
      billedRootInput += inTok;
    } else {
      const aid = readString(p, "agent_id") ?? "";
      const prev = subPeakPerAgent.get(aid) ?? 0;
      if (inTok > prev) subPeakPerAgent.set(aid, inTok);
      subOutputTokens += outTok;
      billedSubInput += inTok;
      if (aid) subAgents.add(aid);
    }
  }
  // Each sub-agent runs in its own isolated context, so summing per-agent
  // peaks tells us "combined peak pressure across parallel sub-contexts".
  let subInputTokens = 0;
  for (const v of subPeakPerAgent.values()) subInputTokens += v;
  if (!peakRootInput && !outputTokens && !subInputTokens && !subOutputTokens) {
    return undefined;
  }
  return {
    inputTokens: peakRootInput,
    outputTokens,
    subInputTokens,
    subOutputTokens,
    subAgentCount: subAgents.size,
    cacheCreationTokens,
    cacheReadTokens,
    reasoningTokens,
    billedInputTokens: billedRootInput + billedSubInput,
  };
}
export function turnHasWidget(timeline: TimelineEntry[], turnId: string): boolean {
  return timeline.some((entry) => entry.turnId === turnId && entry.role === "widget");
}

/**
 * Extract unified-diff files from a `tool_result` event's structured
 * result, appending them to `diffFiles` in place. `buildTimeline` and
 * `getActiveTurn` both accumulate the same per-turn diff-file set from the
 * same event shape — shared here so the two turn-walkers can't drift on
 * this one step (the rest of their state machines stay separate: they close
 * a turn at different points and buildTimeline additionally builds
 * TimelineEntry rows, so merging further would risk exactly the drift the
 * Python-parity test at the top of this file exists to catch).
 */
function accumulateDiffFiles(event: EventRecord, diffFiles: DiffFile[]): void {
  if (event.type !== "tool_result") return;
  const result = event.payload?.result;
  if (typeof result !== "string") return;
  const parsed = parseStructuredResult(result);
  const diffSource = parsed.kind === "diff" ? parsed.text : parsed.kind === "raw" ? parsed.text : "";
  if (diffSource) {
    diffFiles.push(...extractUnifiedDiffs(diffSource));
  }
}

/**
 * Parse a `todos` event payload into {@link TodoMeta}, dropping
 * label-less rows. Returns null when no usable items remain so callers never
 * render an empty (fabricated) card. The CLI's `done` state is normalized to
 * the console's `completed` so either producer renders identically.
 */
function parseTodos(payload: Record<string, unknown> | undefined): TodoMeta | null {
  const rawItems = payload?.items;
  const raw = Array.isArray(rawItems) ? rawItems : [];
  const items: TodoItem[] = [];
  for (const entry of raw) {
    const o = (entry ?? {}) as Record<string, unknown>;
    const label = readString(o, "label")?.trim() ?? "";
    if (!label) continue;
    const rawStatus = readString(o, "status") ?? "pending";
    const status: TodoItemStatus =
      rawStatus === "completed" || rawStatus === "done"
        ? "completed"
        : rawStatus === "in_progress"
          ? "in_progress"
          : "pending";
    items.push({ label, status });
  }
  if (items.length === 0) return null;
  const src = payload?.source;
  const agentId = payload?.agent_id;
  return {
    items,
    source: src === "agent" ? "agent" : src === "plan" ? "plan" : undefined,
    agentId: typeof agentId === "string" ? agentId : undefined,
  };
}

/**
 * Extract the display fields of a `trigger_armed` / `trigger_fired` event
 * payload into {@link TriggerTranscriptMeta}. `kind` falls back to a generic
 * label so an unknown/absent kind still renders a sensible marker.
 */
function parseTriggerEvent(
  payload: Record<string, unknown> | undefined,
  action: "armed" | "fired",
): TriggerTranscriptMeta {
  const p = payload ?? {};
  const triggerId = readString(p, "trigger_id");
  const kind = readString(p, "kind") || "trigger";
  const rawSummary = action === "armed" ? p.summary : p.payload_summary;
  const summary =
    typeof rawSummary === "string" && rawSummary.trim()
      ? rawSummary.trim()
      : undefined;
  return { triggerId, kind, action, summary };
}

/**
 * Extract the display fields of a `context_compacted` event payload into
 * {@link CompactionMeta}. Field names (`mode`, `tokens_saved`) mirror
 * `buildContextCompactedLog` in logs.ts — the trace panel's fuller rendering
 * of the same event — so the two surfaces never drift on what the payload
 * means.
 */
function parseCompaction(payload: Record<string, unknown>): CompactionMeta {
  return {
    mode: readString(payload, "mode") ?? "auto",
    tokensSaved: readNumber(payload, "tokens_saved"),
  };
}

/**
 * The shape of the orchestrator's synthetic assistant closure — a lone
 * parenthesised "(Run …)" clause and nothing else, covering every variant it
 * emits ("(Run interrupted by error: …)", "(Run stopped: …)", "(Run canceled
 * by user)", "(Run ended: …)"). A closure matching this carries nothing the
 * failure card doesn't already render, so it is safe to replace; anything else
 * is prose the model actually returned. Anchored at both ends so a real answer
 * that merely opens with a parenthetical is never mistaken for a placeholder.
 */
const SYNTHETIC_CLOSURE = /^\(Run [\s\S]*\)$/;

export function buildTimeline(events: EventRecord[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  let turnIndex = 0;
  let currentTurnId: string | null = null;
  let turnEvents: EventRecord[] = [];
  let turnStart: string | undefined;
  let diffFiles: DiffFile[] = [];
  let lastModel: string | undefined;
  let turnModel: string | undefined;
  // Fallback source for older sessions where attachment descriptors were
  // only ever written onto the `context` event (not the `user` event
  // itself). Mirrors the lastModel/turnModel tracking above.
  let lastAttachments: AttachmentPayload[] | undefined;
  // Index of the assistant entry that closed the most recent turn, while that
  // turn's `completion` is still pending. The orchestrator appends a synthetic
  // closure BEFORE the completion, so a failure has to reach back and upgrade
  // that entry rather than push its own. Cleared the moment the completion is
  // seen or a new turn opens, so a failure can never reach back past a turn
  // that has already settled.
  let closureIndex: number | null = null;
  // The entry a completion left as its turn's terminal read (the assistant
  // closure, a synthetic placeholder, or a run_failed), plus whether that read
  // is a disposable placeholder. A hook's `outcome_assertion` — appended right
  // AFTER the completion it judges — reads this to mark a would-be-"completed"
  // turn as goal-not-met: a placeholder is replaced in place, a real answer is
  // preserved with the verdict beside it. `synthetic` is captured at the source
  // rather than re-derived, because the two seams mint different placeholder
  // texts (`(Run …)` vs `(run ended)`) and only one matches SYNTHETIC_CLOSURE.
  // Consumed once per completion so multiple assertions don't stack cards.
  let lastCompletion: { index: number; synthetic: boolean } | null = null;
  for (const event of events) {
    if (event.type === "context") {
      const payload = event.payload as
        | { model?: string; attachments?: AttachmentPayload[] }
        | undefined;
      if (payload?.model) lastModel = payload.model;
      if (Array.isArray(payload?.attachments) && payload.attachments.length > 0) {
        lastAttachments = payload.attachments;
      }
      continue;
    }
    if (event.type === "user") {
      // An interior turn that never concluded: neither an `assistant` nor a
      // `completion` event arrived before this next prompt. Materialise it
      // BEFORE the reset below, which would otherwise drop every body event
      // accumulated since the last prompt on the floor and erase the turn from
      // the transcript entirely. Deliberately NOT applied to the trailing open
      // turn — that one has no following `user` event, so it stays open and
      // keeps its in-flight rendering.
      //
      // The outcome is synthesised from the ABSENCE of a closure, so there is
      // no done_reason to read and no text to show: inventing prose for a turn
      // that produced none would put words in the model's mouth.
      if (currentTurnId) {
        const lastTs = turnEvents[turnEvents.length - 1]?.ts;
        entries.push({
          id: `interrupted-${turnIndex}`,
          role: "run_failed",
          content: "",
          turnId: currentTurnId,
          ts: lastTs,
          turn: {
            id: currentTurnId,
            events: turnEvents,
            duration: formatTurnDuration(turnStart, lastTs),
            files: mergeDiffFiles(diffFiles),
            model: turnModel,
            tokenUsage: computeTurnTokenUsage(turnEvents),
          },
          runFailure: { reason: "interrupted", text: "" },
        });
      }
      turnIndex += 1;
      currentTurnId = `turn-${turnIndex}`;
      turnEvents = [event];
      diffFiles = [];
      turnStart = event.ts;
      turnModel = lastModel;
      closureIndex = null;
      // Primary source: the persisted `user` event now carries its own
      // attachments. Fall back to the last-seen `context` event for
      // sessions recorded before that change.
      const userPayload = event.payload as
        | { text?: string; attachments?: AttachmentPayload[] }
        | undefined;
      const attachments =
        userPayload?.attachments && userPayload.attachments.length > 0
          ? userPayload.attachments
          : lastAttachments;
      entries.push({
        id: `user-${turnIndex}`,
        role: "user",
        content: String(event.payload?.text ?? ""),
        turnId: currentTurnId,
        ts: event.ts,
        attachments: attachments && attachments.length > 0 ? attachments : undefined,
      });
      // A `context` event's attachments belong to the ONE turn it precedes —
      // clear so a later attachment-less turn never inherits a stale set.
      lastAttachments = undefined;
      continue;
    }
    // Trigger + termination markers render regardless of turn
    // state: `trigger_fired` and `session_terminated` legitimately arrive
    // between turns (a fired wake, or a terminate after the last turn closed),
    // so they're handled BEFORE the open-turn gate that would otherwise drop
    // them. `trigger_armed` arrives mid-turn but is handled here too so it's
    // never gated out. They are standalone markers — not folded into turnEvents.
    if (event.type === "trigger_armed" || event.type === "trigger_fired") {
      const action = event.type === "trigger_armed" ? "armed" : "fired";
      entries.push({
        id: `trigger-${action}-${event.ts}`,
        role: "trigger",
        content: "",
        turnId: currentTurnId ?? "triggers",
        ts: event.ts,
        trigger: parseTriggerEvent(event.payload, action),
      });
      continue;
    }
    // A session-level recovery the user triggered on a failed run. The runtime
    // records it BETWEEN turns — after the failed turn closed, before the
    // resumed turn's own `user` event — so it ALWAYS arrives with no turn
    // open and is handled here, above the gate that would drop every one of
    // them. Only the two user-driven actions are conversation markers; the
    // tool-use loop's own `halt_no_progress` recovery falls through to the
    // turn's events, where the trace panel already renders it.
    if (event.type === "recovery") {
      const action = readString(event.payload ?? {}, "action");
      if (action === "retry" || action === "continue") {
        entries.push({
          id: `recovery-${event.ts}`,
          role: "recovery",
          content: "",
          turnId: currentTurnId ?? "recovery",
          ts: event.ts,
          recovery: { action },
        });
        continue;
      }
    }
    // The runtime replacing older transcript events with a summary so the
    // next model call stays within its context budget (ContextBuilder in
    // mewbo_core/context.py slices the transcript forward past this marker).
    // The auto/user-triggered path fires between turns — after a turn
    // settles, before the next prompt — so it arrives with no turn open and
    // is handled here, above the gate, same fix class as `recovery` above.
    // A mid-loop compaction (still inside an open turn) is handled too: it
    // renders at the point in the flow where the horizon actually moved,
    // rather than being silently dropped for arriving off-schedule.
    //
    // Filtered to depth 0: a sub-agent compacts its own isolated context,
    // which narrows nothing about what THIS conversation's model sees. That
    // one is already surfaced in the trace panel (labeled with its agent id);
    // showing it here would misattribute a sub-context's narrowing to the
    // main thread the user is reading.
    if (event.type === "context_compacted") {
      const payload = event.payload || {};
      const depth = readNumber(payload, "depth") ?? 0;
      if (depth === 0) {
        entries.push({
          id: `compaction-${event.ts}`,
          role: "compaction",
          content: "",
          turnId: currentTurnId ?? "compaction",
          ts: event.ts,
          compaction: parseCompaction(payload),
        });
      }
      continue;
    }
    if (event.type === "session_terminated") {
      entries.push({
        id: `session-terminated-${event.ts}`,
        role: "session_terminated",
        content: "",
        turnId: "session-terminated",
        ts: event.ts,
      });
      continue;
    }
    // A `completion` for a turn an `assistant` event has ALREADY closed. This
    // is the shape every real failure takes: the orchestrator appends a
    // synthetic closure ("(Run interrupted by error: …)") and only then the
    // completion, so the turn is shut before the completion lands. Handled
    // BEFORE the open-turn gate below, which would otherwise drop the event
    // outright — the same fix class as the trigger events.
    //
    // `done_reason` decides WHETHER the run failed — never the closure's text,
    // which is a backend formatting detail and must not become a frontend
    // contract. What the text does decide is whether the closure is the
    // orchestrator's disposable placeholder or an answer worth keeping, and
    // that distinction has no other signal behind it: the API's boot sweep
    // appends its own terminal completion to an orphaned run, so a session
    // that died between a REAL assistant event and its completion gets a
    // failure completion landing on genuine prose. Blanking that would destroy
    // the only copy of the answer the run produced.
    if (event.type === "completion" && !currentTurnId) {
      if (closureIndex !== null) {
        // Capture placeholder-ness before the failure branch blanks content.
        const closureSynthetic = SYNTHETIC_CLOSURE.test(entries[closureIndex].content);
        const runFailure = parseRunFailure(event.payload);
        if (runFailure) {
          const closure = entries[closureIndex];
          if (SYNTHETIC_CLOSURE.test(closure.content)) {
            // Upgrade in place: one entry per turn, so turn metadata stays
            // intact and the card replaces the placeholder bubble rather than
            // rendering beside it.
            closure.role = "run_failed";
            closure.content = "";
            closure.runFailure = runFailure;
            closure.ts = event.ts;
          } else {
            // A real answer — surface the failure BESIDE it. The turn meta
            // stays on the assistant entry so only one footer renders.
            entries.push({
              id: `run-failed-${event.ts}`,
              role: "run_failed",
              content: "",
              turnId: closure.turnId,
              ts: event.ts,
              runFailure,
            });
          }
        }
        // A following outcome_assertion attaches to this turn's terminal read.
        lastCompletion = { index: closureIndex, synthetic: closureSynthetic };
        closureIndex = null;
      }
      continue;
    }
    // A hook's late verdict that the preceding run missed its goal. It arrives
    // between turns (the completion already closed the turn) and carries no
    // done_reason of its own, so it is handled ABOVE the open-turn gate — like
    // the failure completion — reaching back to the turn's terminal entry.
    if (event.type === "outcome_assertion") {
      const meta = parseOutcomeAssertion(event.payload);
      if (meta && lastCompletion !== null) {
        const target = entries[lastCompletion.index];
        // Only a would-be-"completed" turn needs this. A turn already rendered
        // as a failure keeps its own more-specific classification.
        if (target.role === "assistant") {
          if (lastCompletion.synthetic) {
            // A disposable placeholder — the card replaces it.
            target.role = "run_failed";
            target.content = "";
            target.runFailure = meta;
            target.ts = event.ts;
          } else {
            // The run produced a real answer that nonetheless missed its goal:
            // preserve the answer, surface the verdict beside it.
            entries.push({
              id: `run-failed-${event.ts}`,
              role: "run_failed",
              content: "",
              turnId: target.turnId,
              ts: event.ts,
              runFailure: meta,
            });
          }
        }
        // Consume once — further assertions for the same completion are no-ops.
        lastCompletion = null;
      }
      continue;
    }
    if (!currentTurnId) {
      continue;
    }
    turnEvents.push(event);
    accumulateDiffFiles(event, diffFiles);
    if (event.type === "plan_proposed") {
      const payload = event.payload || {};
      const revision = readNumber(payload, "revision") ?? 0;
      entries.push({
        id: `plan-${revision}`,
        role: "plan",
        content: "",
        turnId: currentTurnId,
        plan: {
          revision,
          status: "pending",
          planPath: readString(payload, "plan_path"),
          planContent: readString(payload, "content") ?? "",
          planSummary: readString(payload, "summary"),
          timestamp: event.ts
        }
      });
      continue;
    }
    if (event.type === "todos") {
      // Authoritative live todo/plan checklist. Upsert ONE
      // card per turn so successive snapshots update the checklist in place
      // instead of stacking a card per emission. Empty lists are dropped —
      // todos are never fabricated.
      const todo = parseTodos(event.payload);
      if (todo) {
        const existing = entries.find(
          (e) => e.role === "todos" && e.turnId === currentTurnId,
        );
        if (existing) {
          existing.todos = todo;
        } else {
          entries.push({
            id: `todos-${currentTurnId}`,
            role: "todos",
            content: "",
            turnId: currentTurnId,
            ts: event.ts,
            todos: todo,
          });
        }
      }
      continue;
    }
    if (event.type === "widget_ready") {
      const payload = event.payload as unknown as WidgetReadyPayload | undefined;
      if (payload) {
        entries.push({
          id: `widget-${event.ts}`,
          role: "widget",
          content: "",
          turnId: currentTurnId,
          ts: event.ts,
          widget: payload,
        });
      }
      continue;
    }
    if (event.type === "plan_approved" || event.type === "plan_rejected") {
      const payload = event.payload || {};
      const revision = readNumber(payload, "revision") ?? 0;
      const nextStatus = event.type === "plan_approved" ? "approved" : "rejected";
      for (let i = entries.length - 1; i >= 0; i -= 1) {
        const entry = entries[i];
        if (entry.role === "plan" && entry.plan?.revision === revision) {
          entry.plan = { ...entry.plan, status: nextStatus };
          break;
        }
      }
      continue;
    }
    if (event.type === "user_question") {
      // A pending ask-user-question group (native human-in-the-loop). Push ONE
      // card keyed by call_id, carrying the call_token the answer POST echoes.
      // Settled below by the matching `user_question_answered` fold, mirroring
      // the plan_proposed → plan_approved pending→settled pattern.
      const payload = event.payload as unknown as UserQuestionPayload | undefined;
      if (payload && typeof payload.call_id === "string") {
        entries.push({
          id: `question-${payload.call_id}`,
          role: "question",
          content: "",
          turnId: currentTurnId,
          ts: event.ts,
          question: {
            callId: payload.call_id,
            callToken:
              typeof payload.call_token === "string" ? payload.call_token : "",
            questions: Array.isArray(payload.questions) ? payload.questions : [],
            status: "pending",
          },
        });
      }
      continue;
    }
    if (event.type === "user_question_answered") {
      // Settle the pending question card in place (status + answers +
      // answered_via), whatever the outcome — mirrors the plan_approved fold.
      const payload = event.payload as unknown as
        | UserQuestionAnsweredPayload
        | undefined;
      if (payload && typeof payload.call_id === "string") {
        for (let i = entries.length - 1; i >= 0; i -= 1) {
          const entry = entries[i];
          if (entry.role === "question" && entry.question?.callId === payload.call_id) {
            entry.question = {
              ...entry.question,
              status: payload.outcome,
              answers:
                payload.outcome === "answered" && Array.isArray(payload.answers)
                  ? payload.answers
                  : undefined,
              answeredVia:
                typeof payload.answered_via === "string"
                  ? payload.answered_via
                  : undefined,
            };
            break;
          }
        }
      }
      continue;
    }
    if (event.type === "assistant") {
      const duration = formatTurnDuration(turnStart, event.ts);
      const turn: TurnMeta = {
        id: currentTurnId,
        events: turnEvents,
        duration,
        files: mergeDiffFiles(diffFiles),
        model: turnModel,
        tokenUsage: computeTurnTokenUsage(turnEvents),
      };
      entries.push({
        id: `assistant-${turnIndex}`,
        role: "assistant",
        content: String(event.payload?.text ?? ""),
        turnId: currentTurnId,
        turn
      });
      closureIndex = entries.length - 1;
      currentTurnId = null;
      turnEvents = [];
      diffFiles = [];
      turnStart = undefined;
    }
    // Defensive fallback: materialise the turn on completion when no
    // prior assistant event has closed it. Handles legacy sessions (and
    // any race where a run terminates without writing a final assistant
    // event) — without this the failed turn's tool_results/agent_messages
    // stay orphaned in turnEvents and are silently discarded when the
    // next user turn resets state.
    if (event.type === "completion" && currentTurnId) {
      const duration = formatTurnDuration(turnStart, event.ts);
      const payload = event.payload as
        | { done_reason?: string; text?: string }
        | undefined;
      const reason = String(payload?.done_reason ?? "");
      // Slash-command-emitted completions carry the rendered body in
      // payload.text — surface it directly so the chat shows the result
      // instead of a generic "(run ended)" placeholder.
      const commandText = reason === "command" ? String(payload?.text ?? "") : "";
      const content =
        commandText ? commandText :
        reason === "canceled" || reason === "cancelled" ? "(run canceled)" :
        "(run ended)";
      const turn: TurnMeta = {
        id: currentTurnId,
        events: turnEvents,
        duration,
        files: mergeDiffFiles(diffFiles),
        model: turnModel,
        tokenUsage: computeTurnTokenUsage(turnEvents),
      };
      // A failed run gets its own role rather than an assistant bubble reading
      // "(run interrupted — see logs)": the failure card renders the error and
      // its recovery actions in place, chronologically, so nobody has to open
      // the trace panel to find out what happened.
      const runFailure = parseRunFailure(event.payload);
      entries.push({
        id: `completion-${turnIndex}`,
        role: runFailure ? "run_failed" : "assistant",
        content: runFailure ? "" : content,
        turnId: currentTurnId,
        ts: event.ts,
        turn,
        runFailure: runFailure ?? undefined,
      });
      // A following outcome_assertion attaches to this just-pushed terminal.
      // The `(run ended)`/`(run canceled)` fallbacks are disposable; a command
      // completion's rendered body (`commandText`) is a real result.
      lastCompletion = { index: entries.length - 1, synthetic: !commandText };
      closureIndex = null;
      currentTurnId = null;
      turnEvents = [];
      diffFiles = [];
      turnStart = undefined;
    }
  }
  return entries;
}

export function getActiveTurn(events: EventRecord[]): TurnMeta | null {
  let turnIndex = 0;
  let currentTurnId: string | null = null;
  let turnEvents: EventRecord[] = [];
  let diffFiles: DiffFile[] = [];
  let lastModel: string | undefined;
  let turnModel: string | undefined;
  for (const event of events) {
    if (event.type === "context") {
      const payload = event.payload as { model?: string } | undefined;
      if (payload?.model) lastModel = payload.model;
      continue;
    }
    if (event.type === "user") {
      turnIndex += 1;
      currentTurnId = `turn-${turnIndex}`;
      turnEvents = [event];
      diffFiles = [];
      turnModel = lastModel;
      continue;
    }
    if (!currentTurnId) {
      continue;
    }
    turnEvents.push(event);
    accumulateDiffFiles(event, diffFiles);
    if (event.type === "assistant") {
      currentTurnId = null;
      turnEvents = [];
      diffFiles = [];
    }
    // Match buildTimeline: a completion event also closes the active turn.
    if (event.type === "completion" && currentTurnId) {
      currentTurnId = null;
      turnEvents = [];
      diffFiles = [];
    }
  }
  if (!currentTurnId) {
    return null;
  }
  return {
    id: currentTurnId,
    events: turnEvents,
    files: mergeDiffFiles(diffFiles),
    model: turnModel,
  };
}
/**
 * Live assistant text for the currently-open turn, assembled from streamed
 * `agent_message_delta` events.
 *
 * The engine emits one `agent_message_delta` per LLM text chunk
 * (`{text, agent_id, depth, step}`) for true time-to-first-token, then the
 * existing per-step `agent_message` once the step's text is final, and finally
 * the turn-closing `assistant`/`completion` event. This coalesces the ROOT
 * agent's (depth 0) deltas into one growing message: deltas accumulate into the
 * in-flight block, and a final `agent_message` replaces that block with its
 * authoritative text (so any delta/serialization drift is reconciled away).
 *
 * Returns "" when there is no open turn or no streamed text — so callers render
 * a graceful no-op (the existing "Working…" beat) for non-streaming models or
 * legacy transcripts. The turn-closing `assistant` bubble (built by
 * {@link buildTimeline}) supersedes this entirely once the turn settles, so the
 * final text is authoritative and never duplicated.
 */
export function getActiveStreamText(events: EventRecord[]): string {
  const turn = getActiveTurn(events);
  if (!turn) {
    return "";
  }
  const finalized: string[] = []; // completed step texts (root, depth 0)
  let buffer = ""; // in-flight delta accumulation for the current step
  for (const event of turn.events) {
    const payload = (event.payload ?? {}) as Record<string, unknown>;
    const depth = readNumber(payload, "depth") ?? 0;
    if (depth !== 0) {
      continue; // only the root agent's narration belongs in the conversation
    }
    if (event.type === "agent_message_delta") {
      const text = readString(payload, "text") ?? "";
      buffer += text;
    } else if (event.type === "agent_message") {
      // The per-step message is authoritative — replace the streamed buffer
      // (drops drift; also covers non-streaming models that emit no deltas).
      const text = readString(payload, "text") ?? "";
      if (text) {
        finalized.push(text);
      }
      buffer = "";
    }
  }
  const parts = buffer ? [...finalized, buffer] : finalized;
  return parts.join("\n\n");
}

/**
 * The session's effective context — the single most-recent `context`-type
 * event's payload, verbatim. Mirrors the backend's `_load_last_context`
 * (apps/mewbo_api/.../backend.py), which is what `/message` re-engage and
 * `/recover` actually read to resume a session: a reverse scan that returns
 * the FIRST (i.e. latest) context event's payload as-is — never merged
 * across events. This matters because InputBar's per-turn context payload
 * OMITS a falsy field (e.g. a cleared `project`) rather than sending it as
 * `null` — folding payloads forward would make a cleared field "stick" from
 * an earlier event.
 *
 * `fallback` (typically the session summary's last-known context) is used
 * only when no context event has been observed yet at all — the transient
 * window before a session's events have loaded — and self-corrects the
 * instant a real context event is seen.
 */
export function getLastContext(
  events: EventRecord[],
  fallback?: SessionContext,
): SessionContext | undefined {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.type === "context") {
      return event.payload as SessionContext;
    }
  }
  return fallback;
}

/** Duration between two ISO timestamps, formatted via the shared
 *  `formatDuration`; undefined when either is missing/unparseable or the span
 *  is non-positive (so a turn with no measurable duration renders nothing). */
function formatTurnDuration(start?: string, end?: string): string | undefined {
  if (!start || !end) {
    return undefined;
  }
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
    return undefined;
  }
  return formatDuration(endMs - startMs);
}
