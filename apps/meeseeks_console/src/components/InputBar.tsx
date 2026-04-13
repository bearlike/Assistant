import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Send,
  Paperclip,
  Check,
  Plus,
} from 'lucide-react';
import { QueryMode, SessionContext } from '../types';
import { useMcpTools } from '../hooks/useMcpTools';
import { useSkills } from '../hooks/useSkills';
import { useProjects } from '../hooks/useProjects';
import { useModels } from '../hooks/useModels';
import { useContainerCompact } from '../hooks/useContainerCompact';
import { ConfigMenu, McpOption, McpStatus } from './ConfigMenu';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';
import { Dialog, DialogContent, DialogTitle } from './ui/dialog';
import { Alert, AlertDescription, AlertTitle } from './ui/alert';
import { Button } from './ui/button';
import { InputComposerBody } from './InputComposerBody';

/** Container base — shared by home & detail mode outer wrapper. */
const INPUT_CONTAINER_BASE =
  'bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-1 shadow-lg ' +
  'transition-all duration-200 ease-out';

/** Container glow — applied via JS state so it stays stable during menu interactions. */
const INPUT_CONTAINER_GLOW =
  'ring-2 ring-[hsl(var(--ring))]/40 ' +
  'shadow-[0_0_20px_hsl(var(--ring)/0.15)] ' +
  'border-[hsl(var(--ring))]/30';

type McpToolOption = McpOption & {
  server?: string;
  disabled_reason?: string;
};
interface InputBarProps {
  mode: 'home' | 'detail';
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
}
export function InputBar({
  mode,
  sessionContext,
  onSubmit,
  onStop,
  isRunning = false,
  isSubmitting = false,
  error,
  onFocusChange
}: InputBarProps) {
  const [isPlusMenuOpen, setIsPlusMenuOpen] = useState(false);
  const [isConfigOpen, setIsConfigOpen] = useState(false);
  const [isFullScreen, setIsFullScreen] = useState(false);
  const [isFocused, setIsFocused] = useState(false);
  const isExpanded = isFocused || isConfigOpen || isPlusMenuOpen;
  const [activeSkill, setActiveSkill] = useState<string | null>(sessionContext?.skill ?? null);
  const [activeProject, setActiveProject] = useState<string | null>(sessionContext?.project ?? null);
  const [activeModel, setActiveModel] = useState<string | null>(sessionContext?.model ?? null);
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
  const [mcps, setMcps] = useState<McpToolOption[]>([]);
  const [inputValue, setInputValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fullScreenTextareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const compact = useContainerCompact(containerRef);
  // Sync local state from session context when navigating between sessions
  useEffect(() => {
    setActiveProject(sessionContext?.project ?? null);
    setActiveSkill(sessionContext?.skill ?? null);
    setActiveModel(sessionContext?.model ?? null);
    setQueryMode(sessionContext?.mode ?? 'act');
    pendingMcpToolsRef.current = sessionContext?.mcp_tools ?? null;
    setMcps([]);
  }, [sessionContext?.project, sessionContext?.skill, sessionContext?.model, sessionContext?.mode, sessionContext?.mcp_tools]);
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
            ?? (sessionToolSet ? sessionToolSet.has(tool.tool_id) : tool.enabled),
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
    setIsPlusMenuOpen(false);
    fileInputRef.current?.click();
  };
  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    setAttachedFiles(files);
    setIsPlusMenuOpen(false);
    event.target.value = '';
  };
  const togglePlanMode = () => {
    setQueryMode((prev) => (prev === 'plan' ? 'act' : 'plan'));
    setIsPlusMenuOpen(false);
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
    setMcps((prev) => prev.map((m) => (m.enabled ? { ...m, active: false } : m)));
  };
  const handleSubmit = () => {
    if (!onSubmit || !inputValue.trim() || isSubmitting) {
      return;
    }
    const modelToSend = activeModel || defaultModel || undefined;
    const context: SessionContext = {
      mcp_tools: mcps.filter((m) => m.active).map((m) => m.id),
      ...(activeSkill ? { skill: activeSkill } : {}),
      ...(activeProject ? { project: activeProject } : {}),
      ...(modelToSend ? { model: modelToSend } : {})
    };
    void onSubmit(inputValue.trim(), context, queryMode, attachedFiles);
    setInputValue('');
    setAttachedFiles([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
    setIsFullScreen(false);
  };
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };
  const renderPlusMenu = (direction: 'up' | 'down') => (
    <DropdownMenu
      open={isPlusMenuOpen}
      onOpenChange={(open) => {
        setIsPlusMenuOpen(open);
        if (open) setIsConfigOpen(false);
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          className={isPlusMenuOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''}
          aria-label="Open menu"
        >
          <Plus className="w-3.5 h-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side={direction === 'up' ? 'top' : 'bottom'} align="start" className="w-48">
        <DropdownMenuLabel className="text-[10px] uppercase tracking-wider text-[hsl(var(--muted-foreground))] font-medium">
          Built-in
        </DropdownMenuLabel>
        {/* Plan mode is locked while a run is in progress — switching mode mid-run
            would change the next turn's intent without affecting the running step. */}
        <DropdownMenuItem
          onSelect={togglePlanMode}
          disabled={isRunning}
          title={isRunning ? 'Plan mode is locked while the agent is running' : undefined}
        >
          <Send className="w-3.5 h-3.5 mr-2" />
          <span className="flex-1">Plan mode</span>
          {queryMode === 'plan' && (
            <Check className="w-3.5 h-3.5 text-[hsl(var(--primary))]" />
          )}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="text-[10px] uppercase tracking-wider text-[hsl(var(--muted-foreground))] font-medium">
          External
        </DropdownMenuLabel>
        <DropdownMenuItem onSelect={handleAttach}>
          <Paperclip className="w-3.5 h-3.5 mr-2" />
          Upload attachment
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
  const renderConfigMenu = (direction: 'up' | 'down') => (
    <ConfigMenu
      mcpOptions={groupedOptions}
      skills={availableSkills}
      projects={availableProjects}
      models={availableModels}
      defaultModel={defaultModel}
      activeProject={activeProject}
      activeSkill={activeSkill}
      activeModel={activeModel}
      mcpLoading={mcpLoading}
      mcpError={mcpError}
      skillsLoading={skillsLoading}
      skillsError={skillsError}
      projectsLoading={projectsLoading}
      projectsError={projectsError}
      modelsLoading={modelsLoading}
      modelsError={modelsError}
      onRefreshMcp={refreshMcp}
      onRefreshSkills={refreshSkills}
      onRefreshProjects={refreshProjects}
      onRefreshModels={refreshModels}
      onToggleMcp={toggleMcp}
      onSelectProject={setActiveProject}
      onSelectSkill={setActiveSkill}
      onSelectModel={setActiveModel}
      onResetAll={handleResetAll}
      isOpen={isConfigOpen}
      onToggleOpen={() => {
        setIsConfigOpen(!isConfigOpen);
        setIsPlusMenuOpen(false);
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
        // Toggling full-screen closes Plus/Config popovers (preserves prior rule).
        if (!open) {
          setIsPlusMenuOpen(false);
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
          onInputChange={setInputValue}
          onSubmit={handleSubmit}
          onKeyDown={handleKeyDown}
          isSubmitting={isSubmitting}
          isExpanded={isExpanded}
          queryMode={queryMode}
          attachedFiles={attachedFiles}
          onClearAttachments={() => setAttachedFiles([])}
          plusMenu={renderPlusMenu('up')}
          configMenu={renderConfigMenu('up')}
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
  // Dialog is open, it owns the single live instance of the plus/config menus
  // — this prevents duplicate portaled popovers fighting for clicks behind
  // the modal overlay (both share isPlusMenuOpen/isConfigOpen state).
  const inlinePlusMenu = isFullScreen ? null : renderPlusMenu(popupDirection);
  const inlineConfigMenu = isFullScreen ? null : renderConfigMenu(popupDirection);

  if (mode === 'home') {
    return (
      <>
        <div className="w-full mb-0 relative" data-testid="inputbar-home">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={handleFileChange}
            className="hidden"
            aria-hidden="true"
          />
          <div className="relative">
            <div ref={containerRef} className={`${INPUT_CONTAINER_BASE} ${isExpanded ? INPUT_CONTAINER_GLOW : ''}`}>
              <InputComposerBody
                variant="home"
                inputValue={inputValue}
                onInputChange={setInputValue}
                onSubmit={handleSubmit}
                onKeyDown={handleKeyDown}
                isSubmitting={isSubmitting}
                isExpanded={isExpanded}
                queryMode={queryMode}
                onToggleFullScreen={() => setIsFullScreen(true)}
                attachedFiles={attachedFiles}
                onClearAttachments={() => setAttachedFiles([])}
                plusMenu={inlinePlusMenu}
                configMenu={inlineConfigMenu}
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
          </div>
        </div>
        {fullScreenOverlay}
      </>
    );
  }

  // While a run is in progress every submit is auto-routed to `sendMessage`
  // (the steer endpoint) by useSessionQuery.send — surface that intent in the
  // placeholder so users understand the input is steering, not queuing a new turn.
  const detailPlaceholder = isRunning
    ? (compact ? "Steer the agent..." : "Steer the running agent (your message joins its queue)…")
    : (compact ? "Ask anything..." : "Request changes or ask a question");

  return (
    <>
      <div
        className="border-t border-[hsl(var(--border-strong))] bg-[hsl(var(--background))] p-4 shadow-[0_-4px_12px_rgba(0,0,0,0.12)]"
        data-testid="inputbar-detail"
      >
        <input
          ref={fileInputRef}
          type="file"
          multiple
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
          <div className={`${INPUT_CONTAINER_BASE} ${isExpanded ? INPUT_CONTAINER_GLOW : ''}`}>
            <InputComposerBody
              variant="detail"
              inputValue={inputValue}
              onInputChange={setInputValue}
              onSubmit={handleSubmit}
              onKeyDown={handleKeyDown}
              isSubmitting={isSubmitting}
              isExpanded={isExpanded}
              isRunning={isRunning}
              onStop={onStop}
              queryMode={queryMode}
              onToggleFullScreen={() => setIsFullScreen(true)}
              attachedFiles={attachedFiles}
              onClearAttachments={() => setAttachedFiles([])}
              plusMenu={inlinePlusMenu}
              configMenu={inlineConfigMenu}
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
        </div>
      </div>
      {fullScreenOverlay}
    </>
  );
}
