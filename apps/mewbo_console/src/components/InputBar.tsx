import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Ban, Lock, Trash2, Unlock } from 'lucide-react';
import { CommandSpec, CreateWorktreeInput, QueryMode, SessionContext } from '../types';
import { SkillSummary } from '../api/contracts';
import { cn } from '../lib/utils';
import { useSessionSpec } from '../hooks/useSessionSpec';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { Button } from './ui/button';
import { useMcpTools } from '../hooks/useMcpTools';
import { useSkills } from '../hooks/useSkills';
import { useProjectFiles } from '../hooks/useProjectFiles';
import { useProjects } from '../hooks/useProjects';
import { useModels } from '../hooks/useModels';
import { useCommands } from '../hooks/useCommands';
import { useContainerCompact } from '../hooks/useContainerCompact';
import { useProjectGit } from '../hooks/useProjectGit';
import { executeCommand } from '../api/client';
import { parseCommandInput } from '../lib/commands';
import { parseMentionInput, spliceMention } from '../lib/mentions';
import { FILE_INPUT_ACCEPT, filterAttachments } from '../lib/attachments';
import { toast } from 'sonner';
import { ConfigMenu, McpOption, McpStatus } from './ConfigMenu';
import { ModelSelector } from './ModelSelector';
import { CommandPalette } from './CommandPalette';
import { FileMentionPicker } from './FileMentionPicker';
import { CommandDialog } from './CommandDialog';
import { ConfirmDialog } from './ui/confirm-dialog';
import { Dialog, DialogContent, DialogTitle } from './ui/dialog';
import { Alert, AlertDescription, AlertTitle } from './ui/alert';
import { InputComposerBody } from './InputComposerBody';

/**
 * Composer card — Tasks' thin layer over the shared `.composer-surface` chrome
 * family (`index.css`). The family owns the border, the focus-within halo, and
 * the running/command tints, driven by the `data-halo`/`data-running`/
 * `data-command` attributes the call site sets on this div; `composerCard()`
 * only adds Tasks' own flex body plus the base elevation, which lifts
 * `--elev-1` → `--elev-2` when expanded (focus or an open menu). The
 * border-colour ramp that used to live here is gone — border colour and all
 * behavioural tints now come from the CSS family, so Tasks, Search, and the
 * ComposerShell siblings read as one system.
 */
function composerCard(state: { expanded: boolean }): string {
  const shadow = state.expanded ? '[box-shadow:var(--elev-2)]' : '[box-shadow:var(--elev-1)]';
  return cn(
    'composer-surface flex flex-col gap-1 rounded-[var(--composer-radius)] bg-[var(--composer-bg)] p-[var(--composer-padding)]',
    shadow,
  );
}

type McpToolOption = McpOption & {
  server?: string;
  disabled_reason?: string;
};

/**
 * The composer's tool control while a purpose-bound session refuses tool
 * overrides. It stands in for the editable Integrations picker and is strictly
 * read-only: it renders the server-bound ceiling (`spec.allowed_tools` — `null`
 * = open, `[]` = no tools, a list = exactly those) and offers ONE explicit
 * unlock. Locked is the default; unlocking is a sanctioned per-run override that
 * re-opens the editable picker and resumes sending `mcp_tools`. The lock /
 * ceiling vocabulary mirrors `SessionBindingPanel`'s `ToolCeiling` so the
 * composer and the binding inspector can never name one ceiling two ways.
 *
 * While locked the send omits `mcp_tools` entirely (never an empty array), so
 * the server's binding stands unchallenged instead of being silently dropped.
 */
function LockedToolCeiling({
  allowedTools,
  onUnlock,
  direction,
  compact,
  disabled,
}: {
  allowedTools: string[] | null;
  onUnlock: () => void;
  direction: 'up' | 'down';
  compact: boolean;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const summary =
    allowedTools === null
      ? 'Open'
      : allowedTools.length === 0
        ? 'No tools'
        : `${allowedTools.length} tool${allowedTools.length === 1 ? '' : 's'}`;
  return (
    <Popover open={disabled ? false : open} onOpenChange={(next) => !disabled && setOpen(next)}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label={
            disabled ? 'Tools bound to session (locked while running)' : 'Tools bound to session'
          }
          title={
            disabled
              ? 'Locked while the agent is running'
              : "Tools are bound to this session's purpose"
          }
          className={cn(
            'flex items-center gap-1.5 h-7 rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-normal text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent',
            compact ? 'px-1.5' : 'px-2.5',
          )}
        >
          <Lock className="w-3.5 h-3.5 text-[hsl(var(--primary-text))]" />
          {!compact && <span className="truncate max-w-[120px]">{summary}</span>}
        </button>
      </PopoverTrigger>
      <PopoverContent
        side={direction === 'up' ? 'top' : 'bottom'}
        align="start"
        className="w-72 p-3 space-y-2"
      >
        <div className="flex items-center gap-1.5">
          <Lock className="w-3.5 h-3.5 text-[hsl(var(--primary-text))]" aria-hidden />
          <span className="text-xs font-medium text-[hsl(var(--foreground))]">
            Tools bound to session
          </span>
        </div>
        <p className="text-xs text-[hsl(var(--muted-foreground))] leading-snug">
          {allowedTools === null
            ? 'This session runs under an open tool ceiling — no MCP tools are withheld.'
            : allowedTools.length === 0
              ? 'This session grants no MCP tools; the server refuses tool overrides.'
              : 'This session is limited to the tools below; the server refuses other overrides.'}
        </p>
        {allowedTools !== null && allowedTools.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {allowedTools.map((id) => (
              <span
                key={id}
                className="inline-flex items-center rounded-md bg-[hsl(var(--muted))]/60 px-1.5 py-0.5 font-mono text-2xs text-[hsl(var(--foreground))]"
              >
                {id}
              </span>
            ))}
          </div>
        )}
        <Button
          variant="neutral"
          size="sm"
          className="w-full justify-center gap-1.5"
          onClick={() => {
            onUnlock();
            setOpen(false);
          }}
        >
          <Unlock className="w-3.5 h-3.5" />
          Unlock tool selection
        </Button>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Snapshot of the live agent run that the composer surfaces in its
 * running-state strip. All fields optional — the strip degrades gracefully
 * when individual signals aren't available yet.
 */
export interface RunStatus {
  /** Short label for what the agent is doing right now (tool name or "Running"). */
  phase?: string;
  /** Number of sub-agents currently in `start` state. */
  agents?: number;
  /** Live root context-window fill in tokens (sessionUsage.root_last_input_tokens). */
  tokens?: number;
  /** Live output throughput (estimated tokens/sec) while streaming. */
  tokPerSec?: number;
  /** ISO timestamp of the last user event — used to compute elapsed wall-clock. */
  lastUserTs?: string;
}

interface InputBarProps {
  mode: 'home' | 'detail';
  sessionId?: string;
  sessionContext?: SessionContext;
  onSubmit?: (
    query: string,
    context?: SessionContext,
    mode?: QueryMode,
    attachments?: File[]
  ) => void;
  onStop?: () => void;
  isRunning?: boolean;
  isSubmitting?: boolean;
  error?: string | null;
  onFocusChange?: (focused: boolean, isEmpty: boolean) => void;
  /** Live run status fed into the composer's running-state strip. */
  runStatus?: RunStatus;
  /**
   * Session is permanently terminated. In detail mode the composer
   * is replaced by a calm "this session is terminated" state — no input, no
   * error residue.
   */
  terminated?: boolean;
}
export function InputBar({
  mode,
  sessionId,
  sessionContext,
  onSubmit,
  onStop,
  isRunning = false,
  isSubmitting = false,
  error,
  onFocusChange,
  runStatus,
  terminated = false,
}: InputBarProps) {
  const [isModelOpen, setIsModelOpen] = useState(false);
  const [isConfigOpen, setIsConfigOpen] = useState(false);
  const [isFullScreen, setIsFullScreen] = useState(false);
  const [isFocused, setIsFocused] = useState(false);
  const isExpanded = isFocused || isConfigOpen || isModelOpen;
  const [activeSkill, setActiveSkill] = useState<string | null>(sessionContext?.skill ?? null);
  const [activeProject, setActiveProject] = useState<string | null>(sessionContext?.project ?? null);
  const [activeModel, setActiveModel] = useState<string | null>(sessionContext?.model ?? null);
  // Opt-in cross-model fallback. ``fallbackEnabled`` gates the feature;
  // ``fallbackModels`` is the ordered chain. Both are per-session run settings
  // that travel in the query ``context`` alongside the model selection. The
  // toggle defaults on when the session was created with a non-empty chain.
  const [fallbackEnabled, setFallbackEnabled] = useState<boolean>(
    (sessionContext?.fallback_models?.length ?? 0) > 0,
  );
  const [fallbackModels, setFallbackModels] = useState<string[]>(
    sessionContext?.fallback_models ?? [],
  );
  // ``activeBranch`` follows the parent repo's HEAD by default; it diverges
  // only when the user explicitly picks a different branch in the composer.
  // ``activeWorktree`` is the project_id of a managed worktree to run the
  // session in (overrides ``activeProject`` on submit). Both are local to
  // the composer and reset when the project changes.
  const [activeBranch, setActiveBranch] = useState<string | null>(sessionContext?.branch ?? null);
  const [activeWorktree, setActiveWorktree] = useState<string | null>(null);
  const pendingMcpToolsRef = useRef<string[] | null>(sessionContext?.mcp_tools ?? null);
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [queryMode, setQueryMode] = useState<QueryMode>(sessionContext?.mode ?? 'act');
  const popupDirection = mode === 'home' ? 'down' : 'up';
  const {
    tools: mcpTools,
    loading: mcpLoading,
    error: mcpError,
    refresh: refreshMcp
  } = useMcpTools(activeProject);
  const {
    skills: availableSkills,
    loading: skillsLoading,
    error: skillsError,
    refresh: refreshSkills
  } = useSkills(activeProject);
  const {
    models: availableModels,
    defaultModel,
    capabilities: modelCapabilities,
    loading: modelsLoading,
    error: modelsError,
    refresh: refreshModels,
  } = useModels();
  const {
    projects: availableProjects,
    loading: projectsLoading,
    error: projectsError,
    refresh: refreshProjects
  } = useProjects();
  // ``projectForGit`` strips out the worktree's id so the git lookup
  // always asks about the *parent* repo — picking a worktree as the
  // session context shouldn't make the picker forget about the other
  // branches and worktrees on the same parent.
  const projectForGit = useMemo(() => {
    if (activeWorktree) {
      const parent = availableProjects.find(
        (p) => p.project_id === activeWorktree && p.is_worktree,
      );
      if (parent?.parent_project_id) {
        return `managed:${parent.parent_project_id}`;
      }
    }
    return activeProject;
  }, [activeProject, activeWorktree, availableProjects]);
  const projectGit = useProjectGit(projectForGit);
  // Default-fill ``activeBranch`` with the parent's current HEAD as soon as
  // the API responds, but only if the user hasn't already made a selection
  // on this project. Worktree picks set ``activeBranch`` directly.
  useEffect(() => {
    if (!projectGit.gitRepo) return;
    if (activeBranch !== null) return;
    if (projectGit.currentBranch) {
      setActiveBranch(projectGit.currentBranch);
    }
  }, [projectGit.gitRepo, projectGit.currentBranch, activeBranch]);
  const [mcps, setMcps] = useState<McpToolOption[]>([]);
  const [inputValue, setInputValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fullScreenTextareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const compact = useContainerCompact(containerRef);

  // ── Purpose-bound tool lock ───────────────────────────────────────────────
  // A purpose-bound session (indexer, search, …) refuses tool overrides — the
  // server drops any `mcp_tools` it sends (`SessionSpec.merge_request_overrides`).
  // Reflect that honestly: when the durable binding is purpose-bound AND the
  // server reports `allowed_tools` non-editable, the composer shows the bound
  // ceiling read-only and OMITS `mcp_tools` from the send, instead of silently
  // sending an override that never lands. Unlocking is a deliberate, sanctioned
  // per-run override; it re-locks on every session switch. An open chat and the
  // home composer never enter this branch, so their send is unchanged.
  const { spec: sessionSpec, editable: sessionEditable } = useSessionSpec(sessionId);
  const [toolsUnlocked, setToolsUnlocked] = useState(false);
  useEffect(() => {
    setToolsUnlocked(false);
  }, [sessionId]);
  const toolBindingLocked =
    mode === 'detail' &&
    sessionSpec?.purpose_bound === true &&
    sessionEditable.allowed_tools !== true;
  const toolsLocked = toolBindingLocked && !toolsUnlocked;

  // ── Slash commands ──────────────────────────────────────────────────────
  const { commands } = useCommands();
  const queryClient = useQueryClient();
  const [commandDialog, setCommandDialog] =
    useState<{ title: string; body: string } | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const parsedCommand = useMemo(() => parseCommandInput(inputValue), [inputValue]);
  const commandModeActive = parsedCommand !== null && Boolean(sessionId);
  const matchedCommand: CommandSpec | undefined = useMemo(() => {
    if (!parsedCommand) return undefined;
    return commands.find((c) => c.name === parsedCommand.name);
  }, [parsedCommand, commands]);

  // Open palette whenever the user is in command mode; close otherwise.
  useEffect(() => {
    setPaletteOpen(commandModeActive);
  }, [commandModeActive]);

  // ── @file mentions ──────────────────────────────────────────────────────
  // Caret position drives the active-token detection. We track it alongside
  // the value (updated on every change + caret-moving key/click) so the
  // mention parser sees where the user actually is, not just the text.
  const [caretPos, setCaretPos] = useState(0);
  const [mentionPickerOpen, setMentionPickerOpen] = useState(false);
  const parsedMention = useMemo(
    () => parseMentionInput(inputValue, caretPos),
    [inputValue, caretPos],
  );
  // `@` and `/` are mutually exclusive — a command always wins (it owns the
  // start of the message), so suppress the mention picker in command mode.
  const mentionModeActive = parsedMention !== null && !commandModeActive;
  const {
    files: mentionFiles,
    attachments: mentionAttachments,
  } = useProjectFiles({
    session: sessionId ?? null,
    project: activeProject,
    enabled: Boolean(sessionId || activeProject),
  });
  useEffect(() => {
    setMentionPickerOpen(mentionModeActive);
  }, [mentionModeActive]);

  // Wrap the parent value setter so a caret-bearing change updates both. The
  // textarea's onChange fires before the browser settles selectionStart only
  // for paste/IME edge cases — close enough; key/click handlers refresh it.
  const handleInputChange = useCallback((value: string) => {
    setInputValue(value);
    const el = textareaRef.current;
    if (el) setCaretPos(el.selectionStart ?? value.length);
    else setCaretPos(value.length);
  }, []);
  const syncCaret = useCallback(() => {
    const el = textareaRef.current;
    if (el) setCaretPos(el.selectionStart ?? 0);
  }, []);

  // Filtered+ranked mention candidates, shared by the picker render and the
  // Enter-to-select shortcut so both pick the SAME top row.
  const mentionQuery = parsedMention?.query ?? '';
  const mentionMatches = useMemo(() => {
    const q = mentionQuery.toLowerCase();
    const atts = (q
      ? mentionAttachments.filter((a) => a.toLowerCase().includes(q))
      : mentionAttachments);
    const fls = (q
      ? mentionFiles.filter((f) => f.toLowerCase().includes(q))
      : mentionFiles);
    // Attachments first (matches the picker's group order).
    return [...atts, ...fls];
  }, [mentionQuery, mentionAttachments, mentionFiles]);

  const insertMention = useCallback(
    (path: string) => {
      if (!parsedMention) return;
      const spliced = spliceMention(inputValue, caretPos, parsedMention, path);
      setInputValue(spliced.value);
      setMentionPickerOpen(false);
      // Restore focus + caret just past the inserted token on the next tick
      // (after React commits the new value).
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (el) {
          el.focus();
          el.setSelectionRange(spliced.caret, spliced.caret);
          setCaretPos(spliced.caret);
        }
      });
    },
    [inputValue, caretPos, parsedMention],
  );

  // Transcript commands ('/compact' and friends) are handled async on the
  // backend: the server writes the user event, flips is_running=true, runs
  // the handler in a thread, and writes completion at the end. Polling
  // picks up every event organically — same machinery a regular message
  // turn uses — so the bubble, run indicator, and compact card all appear
  // and survive page refresh without any browser-side state.
  //
  // Dialog and notification renders stay synchronous; their body feeds the
  // dialog or balloon directly via the response.
  const commandMut = useMutation({
    mutationFn: async (vars: { spec: CommandSpec; args: string[] }) => {
      if (!sessionId) {
        throw new Error('No active session');
      }
      return executeCommand(sessionId, vars.spec.name, vars.args);
    },
    onSuccess: (result) => {
      if (result.render === 'dialog') {
        setCommandDialog({ title: result.title, body: result.body });
      } else if (result.render === 'notification') {
        // Backend already wrote the notification; force the panel/balloon
        // to pick it up immediately instead of waiting for the 30s poll.
        queryClient.invalidateQueries({ queryKey: ['notifications'] });
      } else if (result.render === 'transcript' && sessionId) {
        // Wake the events poll right away so the user bubble (which the
        // backend wrote before spawning the thread) appears with no
        // perceivable delay; the polling loop will see running=true on
        // the next tick and stay alive until completion.
        queryClient.invalidateQueries({ queryKey: ['session-events', sessionId] });
      }
    },
    onError: (err) => {
      setCommandDialog({
        title: 'Command failed',
        body: err instanceof Error ? err.message : String(err),
      });
    },
  });

  const dispatchCommand = useCallback(
    (spec: CommandSpec, args: string[]) => {
      if (!sessionId) return;
      // Optimistic clear so the user sees the command was accepted; the
      // mutation surfaces failure via onError above.
      setInputValue('');
      setPaletteOpen(false);
      commandMut.mutate({ spec, args });
    },
    [commandMut, sessionId],
  );
  // Selecting a skill from the slash palette inserts `/skill-name ` into the
  // composer — it is NOT a command-endpoint dispatch. The orchestrator detects
  // a leading `/skill-name` on a normal message turn and runs the skill (see
  // backend `_resolve_skill`), so the user submits it like any other message.
  const insertSkill = useCallback((skill: SkillSummary) => {
    const next = `/${skill.name} `;
    setInputValue(next);
    setPaletteOpen(false);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(next.length, next.length);
        setCaretPos(next.length);
      }
    });
  }, []);

  // Sync local state from session context when navigating between sessions
  useEffect(() => {
    setActiveProject(sessionContext?.project ?? null);
    setActiveSkill(sessionContext?.skill ?? null);
    setActiveModel(sessionContext?.model ?? null);
    setQueryMode(sessionContext?.mode ?? 'act');
    pendingMcpToolsRef.current = sessionContext?.mcp_tools ?? null;
    setMcps([]);
    setActiveBranch(sessionContext?.branch ?? null);
    setActiveWorktree(null);
    setFallbackModels(sessionContext?.fallback_models ?? []);
    setFallbackEnabled((sessionContext?.fallback_models?.length ?? 0) > 0);
  }, [sessionContext?.project, sessionContext?.skill, sessionContext?.model, sessionContext?.mode, sessionContext?.mcp_tools, sessionContext?.branch, sessionContext?.fallback_models]);

  // If the session context resolves to a managed project that is itself a
  // worktree, lift its id into ``activeWorktree`` so the composer shows
  // both the parent (via ``projectForGit``) and the worktree highlighted.
  useEffect(() => {
    if (!activeProject || !activeProject.startsWith('managed:')) return;
    const id = activeProject.slice('managed:'.length);
    const matched = availableProjects.find((p) => p.project_id === id);
    if (matched?.is_worktree) {
      setActiveWorktree((prev) => (prev === id ? prev : id));
    }
  }, [activeProject, availableProjects]);
  useEffect(() => {
    setMcps((prev) => {
      if (mcpTools.length === 0) {
        // Bail out unconditionally — returning a new `[]` when prev is already
        // empty would still register as a state change (Object.is fails on
        // distinct array refs) and re-trigger this effect via mcpTools
        // identity churn from `q.data ?? []` upstream.
        return prev;
      }
      const prevMap = new Map(prev.map((mcp) => [mcp.id, mcp.active]));
      // On fresh load after session context change, use stored mcp_tools
      const pendingTools = pendingMcpToolsRef.current;
      const sessionToolSet = (prevMap.size === 0 && pendingTools)
        ? new Set(pendingTools)
        : null;
      if (sessionToolSet) {
        pendingMcpToolsRef.current = null;
      }
      return mcpTools.map((tool) => {
        const reason = tool.disabled_reason ?? '';
        const isFailed = reason.toLowerCase().includes('fail') || reason.toLowerCase().includes('error');
        const status: McpStatus = tool.enabled ? 'active' : isFailed ? 'error' : 'disabled';
        return {
          id: tool.tool_id,
          name: tool.name,
          active: prevMap.get(tool.tool_id)
            ?? (sessionToolSet
              ? sessionToolSet.has(tool.tool_id)
              // A capability-gated tool is unusable until its capability is
              // granted, so pre-checking it here would manufacture a false
              // grant — leave it unchecked; the user opts in explicitly.
              : tool.enabled && !tool.requires_capability),
          enabled: tool.enabled,
          server: tool.server,
          disabled_reason: tool.disabled_reason,
          scope: tool.scope,
          status,
          count: undefined,
        };
      });
    });
    // Depend on sessionContext.mcp_tools too: when the user switches to a
    // different session that shares the same project, useMcpTools returns
    // the cached tool list with stable identity, so mcpTools alone would
    // not re-trigger this rebuild. The session-intent change (new mcp_tools
    // array from props) is the reliable signal to re-run and consume the
    // pendingMcpToolsRef set by the sync useEffect above.
  }, [mcpTools, sessionContext?.mcp_tools]);
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  }, [inputValue]);
  const groupedOptions = useMemo(() => {
    const groups = new Map<string, McpToolOption[]>();
    for (const tool of mcps) {
      const groupId = tool.server || tool.name;
      const list = groups.get(groupId) || [];
      list.push(tool);
      groups.set(groupId, list);
    }
    return Array.from(groups.entries()).map(([groupId, tools]) => {
      const selectable = tools.filter((tool) => tool.enabled);
      const active =
      selectable.length > 0 ? selectable.every((tool) => tool.active) : false;
      // Worst status wins: error > disabled > active
      const hasError = tools.some((t) => t.status === 'error');
      const hasDisabled = tools.some((t) => t.status === 'disabled');
      const status: McpStatus = hasError ? 'error' : hasDisabled ? 'disabled' : 'active';
      return {
        id: groupId,
        name: groupId,
        count: tools.length,
        active,
        enabled: selectable.length > 0,
        status,
        scope: tools[0]?.scope,
      } satisfies McpOption;
    });
  }, [mcps]);
  const handleAttach = () => {
    fileInputRef.current?.click();
  };
  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    // Gate at file-selection time. Same policy as the backend; this just
    // surfaces the rejection earlier so the user can fix it before
    // hitting "send" (and the backend re-validates as a safety net).
    const activeModelName = activeModel || defaultModel || null;
    const { accepted, rejected } = filterAttachments(files, {
      model: activeModelName,
      capabilities: modelCapabilities,
    });
    if (rejected.length > 0) {
      const summary = rejected
        .map(({ file, reason }) => `${file.name}: ${reason}`)
        .join('\n');
      toast.error(
        rejected.length === 1
          ? `Attachment rejected — ${rejected[0].file.name}: ${rejected[0].reason}`
          : `${rejected.length} attachments rejected`,
        { description: summary }
      );
    }
    setAttachedFiles(accepted);
    event.target.value = '';
  };
  const togglePlanMode = () => {
    setQueryMode((prev) => (prev === 'plan' ? 'act' : 'plan'));
  };
  const toggleMcp = (groupId: string) => {
    setMcps((prev) => {
      const groupTools = prev.filter(
        (tool) => (tool.server || tool.name) === groupId
      );
      const selectable = groupTools.filter((tool) => tool.enabled);
      const groupActive =
      selectable.length > 0 ? selectable.every((tool) => tool.active) : false;
      return prev.map((tool) => {
        if ((tool.server || tool.name) !== groupId) {
          return tool;
        }
        if (!tool.enabled) {
          return tool;
        }
        return {
          ...tool,
          active: !groupActive
        };
      });
    });
  };
  const handleResetAll = () => {
    setActiveProject(null);
    setActiveSkill(null);
    setActiveModel(null);
    setActiveBranch(null);
    setActiveWorktree(null);
    setFallbackEnabled(false);
    setFallbackModels([]);
    setMcps((prev) => prev.map((m) => (m.enabled ? { ...m, active: false } : m)));
  };

  // Toggle a model in/out of the ordered fallback chain. Selecting appends
  // (preserving click order = priority); re-selecting removes it.
  const handleSelectProject = useCallback((next: string | null) => {
    setActiveProject(next);
    // Switching projects must drop branch/worktree picks — they were
    // anchored to the *old* repo and would otherwise leak into a session
    // run against a completely different working tree.
    setActiveBranch(null);
    setActiveWorktree(null);
  }, []);

  const handleSelectBranch = useCallback((next: string | null) => {
    setActiveBranch(next);
  }, []);

  const handleSelectWorktree = useCallback((next: string | null) => {
    setActiveWorktree(next);
  }, []);

  const handleCreateWorktreeFromMenu = useCallback(
    async (input: CreateWorktreeInput) => {
      try {
        // Pass through the structured payload so the caller's choice of
        // mode (new branch from base vs. reuse existing) reaches the API
        // unchanged. The composer auto-pins to the freshly created
        // worktree on success — that's the implicit "select what I just
        // made" behaviour users expect from a one-click create.
        const wt = await projectGit.createWorktreeFor(input);
        if (wt.project_id) {
          setActiveBranch(wt.branch);
          setActiveWorktree(wt.project_id);
        }
      } catch (err) {
        toast.error(
          err instanceof Error ? err.message : 'Could not create worktree',
        );
      }
    },
    [projectGit],
  );

  // A dirty worktree's delete comes back as a 409 — route it through the
  // shared <ConfirmDialog> (same primitive WorktreesPanel's own dirty-worktree
  // force-delete uses) instead of window.confirm, so the destructive path is
  // still explicit but reads like the rest of the app.
  const [forceDeleteWorktree, setForceDeleteWorktree] = useState<
    { id: string; message: string } | null
  >(null);

  const handleDeleteWorktreeFromMenu = useCallback(
    async (worktreeId: string) => {
      try {
        await projectGit.deleteWorktreeFor(worktreeId, false);
        if (activeWorktree === worktreeId) {
          setActiveWorktree(null);
          setActiveBranch(projectGit.currentBranch);
        }
      } catch (err) {
        const message =
          err instanceof Error ? err.message : 'Could not remove worktree';
        if (/uncommitted|dirty|409/i.test(message)) {
          setForceDeleteWorktree({ id: worktreeId, message });
        } else {
          toast.error(message);
        }
      }
    },
    [projectGit, activeWorktree],
  );

  const handleForceDeleteWorktreeConfirm = useCallback(async () => {
    const target = forceDeleteWorktree;
    if (!target) return;
    try {
      await projectGit.deleteWorktreeFor(target.id, true);
      if (activeWorktree === target.id) {
        setActiveWorktree(null);
        setActiveBranch(projectGit.currentBranch);
      }
    } catch (err2) {
      toast.error(err2 instanceof Error ? err2.message : 'Force remove failed');
    } finally {
      setForceDeleteWorktree(null);
    }
  }, [forceDeleteWorktree, projectGit, activeWorktree]);
  const handleSubmit = () => {
    if (!inputValue.trim()) {
      return;
    }
    // @mention path: when the picker is open with at least one candidate,
    // Enter accepts the top row instead of submitting the turn (mirrors the
    // slash palette's Enter-intercept). Mention mode is checked before the
    // command path but is mutually exclusive with it (a `/` always wins).
    if (mentionPickerOpen && mentionModeActive && mentionMatches.length > 0) {
      insertMention(mentionMatches[0]);
      return;
    }
    // Slash-command path: intercept before the normal submit. Commands
    // bypass the isSubmitting gate — they don't enqueue a turn, they call
    // a dedicated endpoint, so a previous submission shouldn't block them.
    if (commandModeActive && parsedCommand) {
      if (matchedCommand) {
        void dispatchCommand(matchedCommand, parsedCommand.args);
        return;
      }
      // Partial command (e.g. user typed "/co" and pressed Enter): if the
      // palette has filtered to exactly one match, dispatch it. Otherwise
      // surface a brief error so the input doesn't sit silently stuck.
      const prefixMatches = commands.filter((c) =>
        c.name.startsWith(parsedCommand.name),
      );
      if (prefixMatches.length === 1) {
        void dispatchCommand(prefixMatches[0], parsedCommand.args);
      } else {
        setCommandDialog({
          title: 'Unknown command',
          body: `\`/${parsedCommand.name}\` is not a recognized command. Type \`/help\` to list available commands.`,
        });
      }
      return;
    }
    if (isSubmitting || !onSubmit) return;
    const modelToSend = activeModel || defaultModel || undefined;
    // Worktree wins over project — picking a worktree is an explicit
    // "run this session in this directory" gesture and must be reflected
    // verbatim in the session context. The backend's
    // ``_populate_worktree_context`` derives ``repo`` and ``branch`` from
    // the worktree id, so we don't double-send those.
    const projectForContext = activeWorktree
      ? `managed:${activeWorktree}`
      : activeProject;
    const branchForContext =
      !activeWorktree &&
      activeBranch &&
      activeBranch !== projectGit.currentBranch
        ? activeBranch
        : null;
    // Only attach the fallback chain when the user opted in and picked at
    // least one model — empty/disabled is the safe default (no fallback).
    // Drop the primary if it slipped into the chain; it's already tried first.
    const fallbackForContext =
      fallbackEnabled
        ? fallbackModels.filter((m) => m !== modelToSend)
        : [];
    const context: SessionContext = {
      // A purpose-bound session drops tool overrides server-side, so while the
      // ceiling is locked we omit the key entirely (never an empty array — that
      // is itself an override, "grant no tools") and let the binding stand.
      ...(toolsLocked ? {} : { mcp_tools: mcps.filter((m) => m.active).map((m) => m.id) }),
      ...(activeSkill ? { skill: activeSkill } : {}),
      ...(projectForContext ? { project: projectForContext } : {}),
      ...(branchForContext ? { branch: branchForContext } : {}),
      ...(modelToSend ? { model: modelToSend } : {}),
      ...(fallbackForContext.length > 0 ? { fallback_models: fallbackForContext } : {})
    };
    void onSubmit(inputValue.trim(), context, queryMode, attachedFiles);
    setInputValue('');
    setAttachedFiles([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
    setIsFullScreen(false);
  };
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Escape dismisses the open mention picker without touching the run/Stop
    // confirm (the composer's Esc-opens-Stop lives in InputComposerBody and
    // only fires while running).
    if (e.key === 'Escape' && mentionPickerOpen) {
      e.preventDefault();
      setMentionPickerOpen(false);
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
      return;
    }
    // Arrow/Home/End move the caret — refresh our tracked position on the next
    // tick so the mention parser re-evaluates the active token.
    if (
      e.key === 'ArrowLeft' ||
      e.key === 'ArrowRight' ||
      e.key === 'Home' ||
      e.key === 'End'
    ) {
      requestAnimationFrame(syncCaret);
    }
  };
  const renderModelSelector = (direction: 'up' | 'down') => (
    <ModelSelector
      models={availableModels}
      defaultModel={defaultModel}
      activeModel={activeModel}
      loading={modelsLoading}
      error={modelsError}
      onRefresh={refreshModels}
      onSelectModel={setActiveModel}
      fallbackEnabled={fallbackEnabled}
      fallbackModels={fallbackModels}
      onToggleFallbackEnabled={setFallbackEnabled}
      onFallbackModelsChange={setFallbackModels}
      open={isModelOpen}
      onToggleOpen={() => {
        setIsModelOpen(!isModelOpen);
        setIsConfigOpen(false);
      }}
      direction={direction}
      disabled={isRunning}
      compact={compact}
    />
  );
  const renderConfigMenu = (direction: 'up' | 'down') => (
    <ConfigMenu
      // While the tool ceiling is locked the editable Integrations picker would
      // be a second surface for an override the send now drops — hand ConfigMenu
      // an empty tool list so it presents none, and let the read-only
      // `LockedToolCeiling` pill carry the bound ceiling instead.
      mcpOptions={toolsLocked ? [] : groupedOptions}
      skills={availableSkills}
      projects={availableProjects}
      activeProject={activeProject}
      activeSkill={activeSkill}
      mcpLoading={mcpLoading}
      mcpError={mcpError}
      skillsLoading={skillsLoading}
      skillsError={skillsError}
      projectsLoading={projectsLoading}
      projectsError={projectsError}
      onRefreshMcp={refreshMcp}
      onRefreshSkills={refreshSkills}
      onRefreshProjects={refreshProjects}
      onToggleMcp={toggleMcp}
      onSelectProject={handleSelectProject}
      onSelectSkill={setActiveSkill}
      onResetAll={handleResetAll}
      gitRepo={projectGit.gitRepo}
      branches={projectGit.branches}
      currentBranch={projectGit.currentBranch}
      worktrees={projectGit.worktrees}
      activeBranch={activeBranch}
      activeWorktree={activeWorktree}
      gitLoading={projectGit.loading}
      gitMutating={projectGit.mutating}
      gitError={projectGit.error}
      // In-session composer: branches/worktrees are surfaced for context but
      // can't be changed — switching branches mid-run would re-anchor the
      // working tree under the agent's feet, and removing worktrees while a
      // session is bound to one would orphan the run.
      gitReadOnly={mode === 'detail'}
      onSelectBranch={handleSelectBranch}
      onSelectWorktree={handleSelectWorktree}
      onCreateWorktree={(input) => void handleCreateWorktreeFromMenu(input)}
      onDeleteWorktree={(id) => void handleDeleteWorktreeFromMenu(id)}
      onRefreshGit={projectGit.refresh}
      open={isConfigOpen}
      onToggleOpen={() => {
        setIsConfigOpen(!isConfigOpen);
        setIsModelOpen(false);
      }}
      direction={direction}
      compact={compact}
      disabled={isRunning}
    />
  );

  const fullScreenOverlay = (
    <Dialog
      open={isFullScreen}
      onOpenChange={(open) => {
        setIsFullScreen(open);
        // Toggling full-screen closes the model/config popovers (preserves prior rule).
        if (!open) {
          setIsModelOpen(false);
          setIsConfigOpen(false);
        }
      }}
    >
      <DialogContent
        className="max-w-3xl h-[85vh] p-0 gap-0 flex flex-col overflow-hidden"
        data-testid="inputbar-fullscreen"
      >
        <div className="flex items-center px-4 py-2.5 border-b border-[hsl(var(--border))]">
          <DialogTitle className="text-xs text-[hsl(var(--muted-foreground))] uppercase tracking-wider font-normal">
            Compose prompt
          </DialogTitle>
        </div>
        <InputComposerBody
          variant="dialog"
          inputValue={inputValue}
          onInputChange={handleInputChange}
          onSubmit={handleSubmit}
          onKeyDown={handleKeyDown}
          isSubmitting={isSubmitting}
          expanded={isExpanded}
          queryMode={queryMode}
          onTogglePlanMode={togglePlanMode}
          onAttach={handleAttach}
          attachedFiles={attachedFiles}
          onClearAttachments={() => setAttachedFiles([])}
          configMenu={renderConfigMenu('up')}
          modelSelector={renderModelSelector('up')}
          textareaRef={fullScreenTextareaRef}
          placeholder="Write your prompt..."
          ariaLabel="Task description (expanded)"
          showVoice={false}
          showStop={false}
          showMaximize={false}
          autoFocus
          fillHeight
        />
      </DialogContent>
    </Dialog>
  );

  // Mount inline menus ONLY when the fullscreen Dialog is closed. While the
  // Dialog is open, it owns the single live instance of the config/model menus
  // — this prevents duplicate portaled popovers fighting for clicks behind
  // the modal overlay (both share isConfigOpen/isModelOpen state).
  const inlineConfigMenu = isFullScreen ? null : renderConfigMenu(popupDirection);
  const inlineModelSelector = isFullScreen ? null : renderModelSelector(popupDirection);

  // The read-only tool-ceiling pill sits in the footer's config cluster only
  // while the ceiling is locked. Nulled when the fullscreen Dialog owns the
  // single live menu instance (mirrors inlineConfigMenu) so no closed popover
  // lingers behind the modal.
  const lockedToolPill =
    !isFullScreen && toolsLocked ? (
      <LockedToolCeiling
        allowedTools={sessionSpec?.allowed_tools ?? null}
        onUnlock={() => setToolsUnlocked(true)}
        direction={popupDirection}
        compact={compact}
        disabled={isRunning}
      />
    ) : null;

  const forceDeleteWorktreeDialog = (
    <ConfirmDialog
      open={forceDeleteWorktree !== null}
      title="Force-remove worktree?"
      description={
        <>
          {forceDeleteWorktree?.message} Uncommitted or unpushed changes will be lost. This cannot
          be undone.
        </>
      }
      confirmLabel="Force remove"
      pendingLabel="Removing…"
      confirmIcon={<Trash2 className="w-4 h-4" />}
      pending={projectGit.mutating}
      onConfirm={() => void handleForceDeleteWorktreeConfirm()}
      onCancel={() => setForceDeleteWorktree(null)}
    />
  );

  if (mode === 'home') {
    return (
      <>
        <div className="w-full mb-0 relative" data-testid="inputbar-home">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={FILE_INPUT_ACCEPT}
            onChange={handleFileChange}
            className="hidden"
            aria-hidden="true"
          />
          <div className="relative">
            {/* Home composer: `@file` references resolve against the chosen
                project (no session yet). The mention picker anchors the input
                container and opens DOWN (home mode). Slash commands stay
                detail-only — they need a live session to dispatch. */}
            <FileMentionPicker
              open={mentionPickerOpen}
              query={mentionQuery}
              files={mentionFiles}
              attachments={mentionAttachments}
              side="bottom"
              onOpenChange={(v) => setMentionPickerOpen(v && mentionModeActive)}
              onSelect={insertMention}
              anchor={
                <div
                  ref={containerRef}
                  className={composerCard({ expanded: isExpanded })}
                  data-halo="soft"
                  data-running={false}
                  data-command={false}
                >
                  <InputComposerBody
                    variant="home"
                    inputValue={inputValue}
                    onInputChange={handleInputChange}
                    onSubmit={handleSubmit}
                    onKeyDown={handleKeyDown}
                    isSubmitting={isSubmitting}
                    expanded={isExpanded}
                    queryMode={queryMode}
                    onTogglePlanMode={togglePlanMode}
                    onAttach={handleAttach}
                    onToggleFullScreen={() => setIsFullScreen(true)}
                    attachedFiles={attachedFiles}
                    onClearAttachments={() => setAttachedFiles([])}
                    configMenu={inlineConfigMenu}
                    modelSelector={inlineModelSelector}
                    textareaRef={textareaRef}
                    placeholder="Describe a task..."
                    ariaLabel="Task description"
                    showVoice
                    showStop={false}
                    showMaximize
                    onFocus={() => {
                      setIsFocused(true);
                      onFocusChange?.(true, !inputValue.trim());
                    }}
                    onBlur={() => {
                      setIsFocused(false);
                      onFocusChange?.(false, !inputValue.trim());
                    }}
                  />
                </div>
              }
            />
          </div>
        </div>
        {fullScreenOverlay}
        {forceDeleteWorktreeDialog}
      </>
    );
  }

  // A permanently terminated session can never run again — replace the whole
  // composer with a calm, final state. No input, no Stop, no error residue.
  if (terminated) {
    return (
      <div
        className="bg-[hsl(var(--background))] px-4 pt-4 pb-4"
        data-testid="inputbar-terminated"
      >
        <div className="max-w-4xl mx-auto">
          <div
            className="flex items-center gap-2.5 rounded-xl border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 px-4 py-3 text-sm text-[hsl(var(--muted-foreground))]"
            role="status"
          >
            <Ban className="w-4 h-4 shrink-0" aria-hidden />
            <span className="font-medium text-[hsl(var(--foreground))]">
              This session is permanently terminated.
            </span>
            <span className="hidden sm:inline">New messages can&apos;t be sent.</span>
          </div>
        </div>
      </div>
    );
  }

  // While a run is in progress every submit is auto-routed to `sendMessage`
  // (the steer endpoint) by useSessionQuery.send — surface that intent in the
  // placeholder so users understand the input is steering, not queuing a new turn.
  const detailPlaceholder = isRunning
    ? "Steer the run…"
    : (compact ? "Ask anything…" : "Request changes or ask a question");

  return (
    <>
      <div
        className="composer-band-glow bg-[hsl(var(--background))] px-4 pt-4 pb-4"
        data-testid="inputbar-detail"
      >
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={FILE_INPUT_ACCEPT}
          onChange={handleFileChange}
          className="hidden"
          aria-hidden="true"
        />
        <div className="max-w-4xl mx-auto relative" ref={containerRef}>
          {error && (
            <div className="mb-3">
              <Alert variant="destructive">
                <AlertTitle>Request error</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            </div>
          )}
          {/* `/` palette (commands + skills) and the `@` mention picker share
              the SAME anchor (the composer shell). They are mutually exclusive
              by mode, so only one popover is ever open — nesting keeps a single
              composer instance. */}
          <CommandPalette
            open={paletteOpen}
            query={parsedCommand?.name ?? ''}
            commands={commands}
            skills={availableSkills}
            side={popupDirection === 'down' ? 'bottom' : 'top'}
            onOpenChange={(v) => setPaletteOpen(v && commandModeActive)}
            onSelect={(cmd) => {
              // Selecting a palette item (click or Enter on the highlighted
              // row) runs the command directly — the palette is the
              // dispatcher, not just an autocomplete affordance.
              void dispatchCommand(cmd, parsedCommand?.args ?? []);
            }}
            onSelectSkill={insertSkill}
            anchor={
              <FileMentionPicker
                open={mentionPickerOpen}
                query={mentionQuery}
                files={mentionFiles}
                attachments={mentionAttachments}
                side={popupDirection === 'down' ? 'bottom' : 'top'}
                onOpenChange={(v) => setMentionPickerOpen(v && mentionModeActive)}
                onSelect={insertMention}
                anchor={
                  <div
                    className={composerCard({ expanded: isExpanded })}
                    data-halo="soft"
                    data-running={isRunning}
                    data-command={commandModeActive}
                  >
                    <InputComposerBody
                      variant="detail"
                      inputValue={inputValue}
                      onInputChange={handleInputChange}
                      onSubmit={handleSubmit}
                      onKeyDown={handleKeyDown}
                      isSubmitting={isSubmitting}
                      expanded={isExpanded}
                      isRunning={isRunning}
                      runStatus={runStatus}
                      onStop={onStop}
                      queryMode={queryMode}
                      onTogglePlanMode={togglePlanMode}
                      onAttach={handleAttach}
                      onToggleFullScreen={() => setIsFullScreen(true)}
                      attachedFiles={attachedFiles}
                      onClearAttachments={() => setAttachedFiles([])}
                      configMenu={
                        lockedToolPill ? (
                          <>
                            {lockedToolPill}
                            {inlineConfigMenu}
                          </>
                        ) : (
                          inlineConfigMenu
                        )
                      }
                      modelSelector={inlineModelSelector}
                      textareaRef={textareaRef}
                      placeholder={detailPlaceholder}
                      ariaLabel="Session query"
                      showVoice
                      showStop
                      showMaximize
                      onFocus={() => setIsFocused(true)}
                      onBlur={() => setIsFocused(false)}
                    />
                  </div>
                }
              />
            }
          />
        </div>
      </div>
      {fullScreenOverlay}
      {forceDeleteWorktreeDialog}
      <CommandDialog
        open={commandDialog !== null}
        title={commandDialog?.title ?? ''}
        body={commandDialog?.body ?? ''}
        onClose={() => setCommandDialog(null)}
      />
    </>
  );
}
