import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Mic,
  ArrowUp,
  Plus,
  Send,
  Paperclip,
  Check,
  Square,
  Loader2,
  Maximize2,
  Minimize2 } from
'lucide-react';
import { QueryMode, SessionContext } from '../types';
import { useMcpTools } from '../hooks/useMcpTools';
import { useSkills } from '../hooks/useSkills';
import { useProjects } from '../hooks/useProjects';
import { useModels } from '../hooks/useModels';
import { useContainerCompact } from '../hooks/useContainerCompact';
import { ConfigMenu, McpOption, McpStatus } from './ConfigMenu';
import { Popover } from './Popover';
import { Alert, AlertDescription, AlertTitle } from './ui/alert';
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
  const menuRef = useRef<HTMLDivElement>(null);
  const configRef = useRef<HTMLDivElement>(null);
  const overlayMenuRef = useRef<HTMLDivElement>(null);
  const overlayConfigRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fullScreenTextareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const compact = useContainerCompact(containerRef);
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      const target = event.target as Node;
      const inPlus =
        (menuRef.current?.contains(target) ?? false) ||
        (overlayMenuRef.current?.contains(target) ?? false);
      if (!inPlus) {
        setIsPlusMenuOpen(false);
      }
      const inConfig =
        (configRef.current?.contains(target) ?? false) ||
        (overlayConfigRef.current?.contains(target) ?? false);
      if (!inConfig) {
        setIsConfigOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);
  // Reset transient popovers when full-screen toggles
  useEffect(() => {
    setIsPlusMenuOpen(false);
    setIsConfigOpen(false);
  }, [isFullScreen]);
  // Escape closes full-screen overlay
  useEffect(() => {
    if (!isFullScreen) return;
    function handleEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setIsFullScreen(false);
      }
    }
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isFullScreen]);
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
        return prev.length ? prev : [];
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
  const PlusMenu = ({ direction }: { direction: 'up' | 'down' }) =>
  <Popover direction={direction} width="w-48" maxHeight="">
      <div className="px-3 pt-2 pb-1">
        <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
          Built-in
        </span>
      </div>
      <button
      onClick={togglePlanMode}
      className="w-full text-left px-3 py-1.5 text-xs text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] flex items-center gap-2 transition-colors">

        <Send className="w-3.5 h-3.5" />
        <span className="flex-1">Plan mode</span>
        {queryMode === 'plan' &&
        <Check className="w-3.5 h-3.5 text-[hsl(var(--primary))]" />
        }
      </button>
      <div className="h-px bg-[hsl(var(--border))] mx-2 my-1" />
      <div className="px-3 pt-1 pb-1">
        <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
          External
        </span>
      </div>
      <button
      onClick={handleAttach}
      className="w-full text-left px-3 py-1.5 text-xs text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] flex items-center gap-2 transition-colors">

        <Paperclip className="w-3.5 h-3.5" />
        Upload attachment
      </button>
    </Popover>;

  const closeOverlayIfBackdrop = (event: React.MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) {
      setIsFullScreen(false);
    }
  };
  const renderConfigMenu = (
    ref: React.RefObject<HTMLDivElement>,
    direction: 'up' | 'down',
  ) => (
    <ConfigMenu
      ref={ref}
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
    />
  );

  const fullScreenOverlay = isFullScreen && (
    <div
      className="fixed inset-0 bg-black/60 backdrop-blur-sm z-[60] flex items-center justify-center p-4"
      onMouseDown={closeOverlayIfBackdrop}
      data-testid="inputbar-fullscreen">
      <div className="w-full max-w-3xl h-[85vh] max-h-[85vh] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl shadow-2xl flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-[hsl(var(--border))]">
          <span className="text-xs text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
            Compose prompt
          </span>
          <button
            type="button"
            onClick={() => setIsFullScreen(false)}
            aria-label="Minimize editor"
            className="p-1 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] transition-colors">
            <Minimize2 className="w-4 h-4" />
          </button>
        </div>
        {attachedFiles.length > 0 &&
        <div className="px-4 pt-3 pb-0">
            <div className="bg-[hsl(var(--accent))] rounded px-2 py-1 text-xs text-[hsl(var(--foreground))] inline-flex items-center gap-2 w-fit">
              <Paperclip className="w-3 h-3" />
              <span className="truncate max-w-[240px]">{attachedFiles[0].name}</span>
              {attachedFiles.length > 1 &&
              <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                  +{attachedFiles.length - 1}
                </span>
              }
              <button
                onClick={() => setAttachedFiles([])}
                className="hover:opacity-70">
                ×
              </button>
            </div>
          </div>
        }
        <div className="flex-1 px-4 py-3 overflow-hidden">
          <textarea
            ref={fullScreenTextareaRef}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Write your prompt..."
            aria-label="Task description (expanded)"
            disabled={isSubmitting}
            autoFocus
            className="w-full h-full bg-transparent border-none outline-none text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] text-base resize-none" />
        </div>
        <div className="flex items-center justify-between px-3 py-2 border-t border-[hsl(var(--border))]">
          <div className="flex items-center gap-2 min-w-0 flex-nowrap">
            <div className="relative" ref={overlayMenuRef}>
              <button
                onClick={() => {
                  setIsPlusMenuOpen(!isPlusMenuOpen);
                  setIsConfigOpen(false);
                }}
                className={`p-1.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-lg transition-colors ${isPlusMenuOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''}`}>
                <Plus className="w-4 h-4" />
              </button>
              {isPlusMenuOpen && <PlusMenu direction="up" />}
            </div>
            {queryMode === 'plan' &&
            <span className="text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10 border border-[hsl(var(--primary))]/30 px-2 py-0.5 rounded-full">
                Plan
              </span>
            }
            {renderConfigMenu(overlayConfigRef, 'up')}
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              onClick={handleSubmit}
              aria-label="Send query"
              disabled={isSubmitting || !inputValue.trim()}
              className={`p-2 rounded-full transition-colors ${isSubmitting || !inputValue.trim() ? 'bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]' : 'bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] hover:opacity-90'}`}>
              {isSubmitting ?
              <Loader2 className="w-4 h-4 animate-spin" /> :
              <ArrowUp className="w-4 h-4" />
              }
            </button>
          </div>
        </div>
      </div>
    </div>
  );

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
          aria-hidden="true" />

        <div className="relative group">
          <div ref={containerRef} className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-1 shadow-lg transition-all focus-within:ring-1 focus-within:ring-[hsl(var(--ring))]/30">
            {attachedFiles.length > 0 &&
            <div className="px-4 pt-3 pb-0 flex items-center gap-2">
                <div className="bg-[hsl(var(--accent))] rounded px-2 py-1 text-xs text-[hsl(var(--foreground))] flex items-center gap-2">
                  <Paperclip className="w-3 h-3" />
                  <span className="truncate max-w-[180px]">{attachedFiles[0].name}</span>
                  {attachedFiles.length > 1 &&
                  <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                      +{attachedFiles.length - 1}
                    </span>
                  }
                  <button
                  onClick={() => setAttachedFiles([])}
                  className="hover:opacity-70">

                    ×
                  </button>
                </div>
              </div>
            }

            <div className="relative px-4 py-3">
              <textarea
                ref={textareaRef}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
                onFocus={() => onFocusChange?.(true, !inputValue.trim())}
                onBlur={() => onFocusChange?.(false, !inputValue.trim())}
                placeholder="Describe a task..."
                aria-label="Task description"
                disabled={isSubmitting}
                rows={1}
                className="w-full bg-transparent border-none outline-none text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] text-base resize-none min-h-[24px] max-h-[200px] pr-7" />
              <button
                type="button"
                onClick={() => setIsFullScreen(true)}
                aria-label="Expand editor"
                className="absolute top-2 right-2 p-1 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] opacity-60 hover:opacity-100 transition-opacity">
                <Maximize2 className="w-3.5 h-3.5" />
              </button>
            </div>

            <div className="flex items-center justify-between px-2 pb-1">
              <div className="flex items-center gap-2 min-w-0 flex-nowrap">
                <div className="relative" ref={menuRef}>
                  <button
                    onClick={() => {
                      setIsPlusMenuOpen(!isPlusMenuOpen);
                      setIsConfigOpen(false);
                    }}
                    className={`p-1.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-lg transition-colors ${isPlusMenuOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''}`}>

                    <Plus className="w-4 h-4" />
                  </button>
                  {isPlusMenuOpen && <PlusMenu direction={popupDirection} />}
                </div>
                {queryMode === 'plan' &&
                <span className="text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10 border border-[hsl(var(--primary))]/30 px-2 py-0.5 rounded-full">
                    Plan
                  </span>
                }

                {renderConfigMenu(configRef, popupDirection)}
              </div>

              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  aria-label="Voice input"
                  className="p-2 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-full transition-colors">

                  <Mic className="w-4 h-4" />
                </button>
                <button
                  onClick={handleSubmit}
                  aria-label="Send query"
                  disabled={isSubmitting || !inputValue.trim()}
                  className={`p-2 rounded-full transition-colors ${isSubmitting || !inputValue.trim() ? 'bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]' : 'bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] hover:opacity-90'}`}>

                  {isSubmitting ?
                  <Loader2 className="w-4 h-4 animate-spin" /> :

                  <ArrowUp className="w-4 h-4" />
                  }
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
      {fullScreenOverlay}
      </>);

  }
  const detailPlaceholder = isRunning
    ? (compact ? "Send a message..." : "Send a message to the running session...")
    : (compact ? "Ask anything..." : "Request changes or ask a question");

  return (
    <>
    <div
      className="border-t border-[hsl(var(--border-strong))] bg-[hsl(var(--background))] p-4 shadow-[0_-4px_12px_rgba(0,0,0,0.12)]"
      data-testid="inputbar-detail">

      <input
        ref={fileInputRef}
        type="file"
        multiple
        onChange={handleFileChange}
        className="hidden"
        aria-hidden="true" />

      <div className="max-w-4xl mx-auto relative" ref={containerRef}>
        {error &&
        <div className="mb-3">
            <Alert variant="destructive">
              <AlertTitle>Request error</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          </div>
        }
        <div className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-1 shadow-lg">
          {attachedFiles.length > 0 &&
          <div className="px-3 pt-2 pb-0">
              <div className="bg-[hsl(var(--accent))] rounded px-2 py-0.5 text-xs text-[hsl(var(--foreground))] inline-flex items-center gap-2 w-fit">
                <Paperclip className="w-3 h-3" />
                <span className="truncate max-w-[180px]">{attachedFiles[0].name}</span>
                {attachedFiles.length > 1 &&
                <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                    +{attachedFiles.length - 1}
                  </span>
                }
                <button
                onClick={() => setAttachedFiles([])}
                className="hover:opacity-70">
                  ×
                </button>
              </div>
            </div>
          }

          <div className="relative px-3 py-2">
            <textarea
              ref={textareaRef}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={detailPlaceholder}
              aria-label="Session query"
              disabled={isSubmitting}
              rows={1}
              className="w-full bg-transparent border-none outline-none text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] text-sm resize-none min-h-[24px] max-h-[200px] pr-7" />
            <button
              type="button"
              onClick={() => setIsFullScreen(true)}
              aria-label="Expand editor"
              className="absolute top-1 right-1 p-1 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] opacity-60 hover:opacity-100 transition-opacity">
              <Maximize2 className="w-3.5 h-3.5" />
            </button>
          </div>

          <div className="flex items-center justify-between px-1 pb-1">
            <div className="flex items-center gap-1 min-w-0 flex-nowrap">
              <div className="relative" ref={menuRef}>
                <button
                  onClick={() => {
                    setIsPlusMenuOpen(!isPlusMenuOpen);
                    setIsConfigOpen(false);
                  }}
                  className={`p-1.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-lg transition-colors ${isPlusMenuOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''}`}>
                  <Plus className="w-4 h-4" />
                </button>
                {isPlusMenuOpen && <PlusMenu direction={popupDirection} />}
              </div>
              {queryMode === 'plan' &&
              <span className="text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10 border border-[hsl(var(--primary))]/30 px-2 py-0.5 rounded-full">
                  Plan
                </span>
              }

              {renderConfigMenu(configRef, popupDirection)}
            </div>

            <div className="flex items-center gap-1 flex-shrink-0">
              <button
                aria-label="Voice input"
                className="p-1.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-full transition-colors">
                <Mic className="w-4 h-4" />
              </button>
              {isRunning &&
              <button
                onClick={onStop}
                aria-label="Stop run"
                className="p-1.5 bg-red-600/30 text-red-300 rounded-full hover:bg-red-600/50 transition-colors">
                  <Square className="w-4 h-4" />
                </button>
              }
              <button
                onClick={handleSubmit}
                aria-label="Send query"
                disabled={isSubmitting || !inputValue.trim()}
                className={`p-1.5 rounded-full transition-colors ${isSubmitting || !inputValue.trim() ? 'bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]' : 'bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] hover:opacity-90'}`}>
                  {isSubmitting ?
                <Loader2 className="w-4 h-4 animate-spin" /> :
                <ArrowUp className="w-4 h-4" />
                }
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
    {fullScreenOverlay}
    </>);

}
