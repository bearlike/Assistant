import React, { useEffect, useMemo, useState, useRef } from 'react';
import {
  Mic,
  ArrowUp,
  Plus,
  Send,
  Paperclip,
  Check,
  Square,
  Loader2 } from
'lucide-react';
import { QueryMode, SessionContext } from '../types';
import { useMcpTools } from '../hooks/useMcpTools';
import { useSkills } from '../hooks/useSkills';
import { useProjects } from '../hooks/useProjects';
import { useContainerCompact } from '../hooks/useContainerCompact';
import { McpSelector, McpOption, McpStatus } from './McpSelector';
import { SkillSelector } from './SkillSelector';
import { ProjectSelector } from './ProjectSelector';
import { Popover } from './Popover';
import { Alert, AlertDescription, AlertTitle } from './ui/alert';
type McpToolOption = McpOption & {
  server?: string;
  disabled_reason?: string;
};
interface InputBarProps {
  mode: 'home' | 'detail';
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
}
export function InputBar({
  mode,
  onSubmit,
  onStop,
  isRunning = false,
  isSubmitting = false,
  error
}: InputBarProps) {
  const [isPlusMenuOpen, setIsPlusMenuOpen] = useState(false);
  const [isMcpOpen, setIsMcpOpen] = useState(false);
  const [isSkillOpen, setIsSkillOpen] = useState(false);
  const [isProjectOpen, setIsProjectOpen] = useState(false);
  const [activeSkill, setActiveSkill] = useState<string | null>(null);
  const [activeProject, setActiveProject] = useState<string | null>(null);
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [queryMode, setQueryMode] = useState<QueryMode>('act');
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
    projects: availableProjects,
    loading: projectsLoading,
    error: projectsError,
    refresh: refreshProjects
  } = useProjects();
  const [mcps, setMcps] = useState<McpToolOption[]>([]);
  const [inputValue, setInputValue] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);
  const mcpRef = useRef<HTMLDivElement>(null);
  const skillRef = useRef<HTMLDivElement>(null);
  const projectRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const compact = useContainerCompact(containerRef);
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setIsPlusMenuOpen(false);
      }
      if (mcpRef.current && !mcpRef.current.contains(event.target as Node)) {
        setIsMcpOpen(false);
      }
      if (skillRef.current && !skillRef.current.contains(event.target as Node)) {
        setIsSkillOpen(false);
      }
      if (projectRef.current && !projectRef.current.contains(event.target as Node)) {
        setIsProjectOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);
  useEffect(() => {
    setMcps((prev) => {
      if (mcpTools.length === 0) {
        return prev.length ? prev : [];
      }
      const prevMap = new Map(prev.map((mcp) => [mcp.id, mcp.active]));
      return mcpTools.map((tool) => {
        const reason = tool.disabled_reason ?? '';
        const isFailed = reason.toLowerCase().includes('fail') || reason.toLowerCase().includes('error');
        const status: McpStatus = tool.enabled ? 'active' : isFailed ? 'error' : 'disabled';
        return {
          id: tool.tool_id,
          name: tool.name,
          active: prevMap.get(tool.tool_id) ?? tool.enabled,
          enabled: tool.enabled,
          server: tool.server,
          disabled_reason: tool.disabled_reason,
          scope: tool.scope,
          status,
          count: undefined,
        };
      });
    });
  }, [mcpTools]);
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
  const handleSubmit = () => {
    if (!onSubmit || !inputValue.trim() || isSubmitting) {
      return;
    }
    const context: SessionContext = {
      mcp_tools: mcps.filter((m) => m.active).map((m) => m.id),
      ...(activeSkill ? { skill: activeSkill } : {}),
      ...(activeProject ? { project: activeProject } : {})
    };
    void onSubmit(inputValue.trim(), context, queryMode, attachedFiles);
    setInputValue('');
    setAttachedFiles([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };
  const PlusMenu = () =>
  <Popover direction={popupDirection} width="w-48" maxHeight="">
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

  if (mode === 'home') {
    return (
      <div className="w-full mb-0 relative" data-testid="inputbar-home">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          onChange={handleFileChange}
          className="hidden"
          aria-hidden="true" />

        <div className="relative group">
          <div className="bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-xl p-1 shadow-lg transition-all focus-within:ring-1 focus-within:ring-[hsl(var(--ring))]/30">
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

            <div className="px-4 py-3">
              <textarea
                ref={textareaRef}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Describe a task..."
                aria-label="Task description"
                disabled={isSubmitting}
                rows={1}
                className="w-full bg-transparent border-none outline-none text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] text-base resize-none min-h-[24px] max-h-[200px]" />

            </div>

            <div className="flex items-center justify-between px-2 pb-1">
              <div className="flex items-center gap-2">
                <div className="relative" ref={menuRef}>
                  <button
                    onClick={() => {
                      setIsPlusMenuOpen(!isPlusMenuOpen);
                      setIsMcpOpen(false);
                    }}
                    className={`p-1.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-lg transition-colors ${isPlusMenuOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''}`}>

                    <Plus className="w-4 h-4" />
                  </button>
                  {isPlusMenuOpen && <PlusMenu />}
                </div>
                {queryMode === 'plan' &&
                <span className="text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10 border border-[hsl(var(--primary))]/30 px-2 py-0.5 rounded-full">
                    Plan
                  </span>
                }

                <McpSelector
                  ref={mcpRef}
                  options={groupedOptions}
                  isOpen={isMcpOpen}
                  loading={mcpLoading}
                  error={mcpError}
                  direction={popupDirection}
                  onToggleOpen={() => {
                    setIsMcpOpen(!isMcpOpen);
                    setIsPlusMenuOpen(false);
                    setIsSkillOpen(false);
                  }}
                  onToggle={toggleMcp}
                  onRefresh={refreshMcp} />

                <SkillSelector
                  ref={skillRef}
                  skills={availableSkills}
                  activeSkill={activeSkill}
                  isOpen={isSkillOpen}
                  loading={skillsLoading}
                  error={skillsError}
                  direction={popupDirection}
                  onToggleOpen={() => {
                    setIsSkillOpen(!isSkillOpen);
                    setIsMcpOpen(false);
                    setIsPlusMenuOpen(false);
                    setIsProjectOpen(false);
                  }}
                  onSelect={(name) => {
                    setActiveSkill(name);
                    setIsSkillOpen(false);
                  }}
                  onRefresh={refreshSkills} />

                <ProjectSelector
                  ref={projectRef}
                  projects={availableProjects}
                  activeProject={activeProject}
                  isOpen={isProjectOpen}
                  loading={projectsLoading}
                  error={projectsError}
                  direction={popupDirection}
                  onToggleOpen={() => {
                    setIsProjectOpen(!isProjectOpen);
                    setIsMcpOpen(false);
                    setIsPlusMenuOpen(false);
                    setIsSkillOpen(false);
                  }}
                  onSelect={(name) => {
                    setActiveProject(name);
                    setIsProjectOpen(false);
                  }}
                  onRefresh={refreshProjects} />

              </div>

              <div className="flex items-center gap-2">
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
      </div>);

  }
  const detailPlaceholder = isRunning
    ? (compact ? "Send a message..." : "Send a message to the running session...")
    : (compact ? "Ask anything..." : "Request changes or ask a question");

  return (
    <div
      className="border-t border-[hsl(var(--border))] bg-[hsl(var(--background))] p-4"
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

          <div className="px-3 py-2">
            <textarea
              ref={textareaRef}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={detailPlaceholder}
              aria-label="Session query"
              disabled={isSubmitting}
              rows={1}
              className="w-full bg-transparent border-none outline-none text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] text-sm resize-none min-h-[24px] max-h-[200px]" />
          </div>

          <div className="flex items-center justify-between px-1 pb-1">
            <div className="flex items-center gap-1">
              <div className="relative" ref={menuRef}>
                <button
                  onClick={() => {
                    setIsPlusMenuOpen(!isPlusMenuOpen);
                    setIsMcpOpen(false);
                  }}
                  className={`p-1.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-lg transition-colors ${isPlusMenuOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''}`}>
                  <Plus className="w-4 h-4" />
                </button>
                {isPlusMenuOpen && <PlusMenu />}
              </div>
              {queryMode === 'plan' &&
              <span className="text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10 border border-[hsl(var(--primary))]/30 px-2 py-0.5 rounded-full">
                  Plan
                </span>
              }

              <McpSelector
                ref={mcpRef}
                options={groupedOptions}
                isOpen={isMcpOpen}
                loading={mcpLoading}
                error={mcpError}
                direction={popupDirection}
                compact={compact}
                onToggleOpen={() => {
                  setIsMcpOpen(!isMcpOpen);
                  setIsPlusMenuOpen(false);
                  setIsSkillOpen(false);
                }}
                onToggle={toggleMcp}
                onRefresh={refreshMcp} />

              <SkillSelector
                ref={skillRef}
                skills={availableSkills}
                activeSkill={activeSkill}
                isOpen={isSkillOpen}
                loading={skillsLoading}
                error={skillsError}
                direction={popupDirection}
                compact={compact}
                onToggleOpen={() => {
                  setIsSkillOpen(!isSkillOpen);
                  setIsMcpOpen(false);
                  setIsPlusMenuOpen(false);
                  setIsProjectOpen(false);
                }}
                onSelect={(name) => {
                  setActiveSkill(name);
                  setIsSkillOpen(false);
                }}
                onRefresh={refreshSkills} />

              <ProjectSelector
                ref={projectRef}
                projects={availableProjects}
                activeProject={activeProject}
                isOpen={isProjectOpen}
                loading={projectsLoading}
                error={projectsError}
                direction={popupDirection}
                compact={compact}
                onToggleOpen={() => {
                  setIsProjectOpen(!isProjectOpen);
                  setIsMcpOpen(false);
                  setIsPlusMenuOpen(false);
                  setIsSkillOpen(false);
                }}
                onSelect={(name) => {
                  setActiveProject(name);
                  setIsProjectOpen(false);
                }}
                onRefresh={refreshProjects} />
            </div>

            <div className="flex items-center gap-1">
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
    </div>);

}
