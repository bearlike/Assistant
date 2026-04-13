import { useEffect, useMemo, useState } from 'react';
import {
  Blocks,
  ChevronDown,
  ChevronUp,
  Circle,
  Cpu,
  FolderOpen,
  RefreshCw,
  RotateCcw,
  Sliders,
  Zap,
} from 'lucide-react';
import { ProjectSummary, SkillSummary } from '../api/client';
import { getProviderIcon } from '../utils/modelIcon';
import { isUnsupportedModel } from '../utils/modelSupport';
import { ModelBrandIcon } from './ModelBrandIcon';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from './ui/command';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';

export type McpStatus = 'active' | 'disabled' | 'error';

export type McpOption = {
  id: string;
  name: string;
  active: boolean;
  enabled: boolean;
  count?: number;
  status: McpStatus;
  scope?: string;
};

type ConfigMenuProps = {
  // Data
  mcpOptions: McpOption[];
  skills: SkillSummary[];
  projects: ProjectSummary[];
  models: string[];
  defaultModel: string | null;
  // Active state
  activeProject: string | null;
  activeSkill: string | null;
  activeModel: string | null;
  // Loading/error per category
  mcpLoading: boolean;
  mcpError: string | null;
  skillsLoading: boolean;
  skillsError: string | null;
  projectsLoading: boolean;
  projectsError: string | null;
  modelsLoading: boolean;
  modelsError: string | null;
  // Refresh callbacks
  onRefreshMcp: () => void;
  onRefreshSkills: () => void;
  onRefreshProjects: () => void;
  onRefreshModels: () => void;
  // Selection callbacks
  onToggleMcp: (id: string) => void;
  onSelectProject: (name: string | null) => void;
  onSelectSkill: (name: string | null) => void;
  onSelectModel: (name: string | null) => void;
  onResetAll: () => void;
  // Popover control
  isOpen: boolean;
  onToggleOpen: () => void;
  direction?: 'up' | 'down';
  compact?: boolean;
  /** When true, the trigger button is non-interactive (used while a session is
   * running — selections are locked while the agent steers the existing context). */
  disabled?: boolean;
};

type Tab = 'root' | 'project' | 'model' | 'skills' | 'mcps';

const DOT_CLASSES: Record<McpStatus, string> = {
  active: 'fill-emerald-500 text-emerald-500',
  disabled: 'fill-amber-500 text-amber-500',
  error: 'fill-red-500 text-red-500',
};

/** Truncate a model ID for display (e.g. "openai/gpt-4o-mini" → "gpt-4o-mini"). */
function shortModel(id: string): string {
  const slash = id.lastIndexOf('/');
  return slash >= 0 ? id.slice(slash + 1) : id;
}

/** Selection key for a project entry — managed projects use a `managed:` prefix. */
function projectKey(p: ProjectSummary): string {
  return p.source === 'managed' && p.project_id
    ? `managed:${p.project_id}`
    : p.name;
}

// Visual overrides to restore the dense pre-migration look on top of shadcn's
// roomier desktop-command-palette defaults. Behavior (search, keyboard nav,
// selection state) still comes from cmdk via shadcn — only typography/spacing
// is being re-themed here.
const COMMAND_INPUT_CLS = 'h-7 py-1 text-xs';
const COMMAND_EMPTY_CLS = 'py-3 text-xs text-[hsl(var(--muted-foreground))]';
const COMMAND_GROUP_CLS =
  'p-0 [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-[hsl(var(--muted-foreground))]';
// Two-line item (e.g., project name + description). Override shadcn's
// `gap-2 items-center` (single-line) and `text-sm px-2 py-1.5` (roomy) with
// `flex-col items-start gap-0 text-xs px-3 py-2` (dense vertical stack).
const COMMAND_ITEM_TWO_LINE_CLS =
  'flex-col items-start gap-0 px-3 py-2 text-xs rounded-none';
// Single-line item (e.g., "Default", "Temporary directory", "None").
const COMMAND_ITEM_SINGLE_LINE_CLS = 'px-3 py-2 text-xs rounded-none';

function StatusDot({ status, active }: { status: McpStatus; active: boolean }) {
  if (status === 'active' && !active) {
    return <Circle className="w-2 h-2 shrink-0 fill-[hsl(var(--muted))] text-[hsl(var(--border))]" />;
  }
  return <Circle className={`w-2 h-2 shrink-0 ${DOT_CLASSES[status]}`} />;
}

function RefreshIcon({ onRefresh, label }: { onRefresh?: () => void; label: string }) {
  if (!onRefresh) return null;
  return (
    <button
      onClick={onRefresh}
      aria-label={label}
      className="p-0.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
      type="button"
    >
      <RefreshCw className="w-3 h-3" />
    </button>
  );
}

function ErrorBanner({ error }: { error?: string | null }) {
  if (!error) return null;
  return (
    <div className="px-3 py-2 text-xs text-red-500 border-b border-[hsl(var(--border))] bg-red-500/5">
      {error}
    </div>
  );
}

export function ConfigMenu({
  mcpOptions,
  skills,
  projects,
  models,
  defaultModel,
  activeProject,
  activeSkill,
  activeModel,
  mcpLoading,
  mcpError,
  skillsLoading,
  skillsError,
  projectsLoading,
  projectsError,
  modelsLoading,
  modelsError,
  onRefreshMcp,
  onRefreshSkills,
  onRefreshProjects,
  onRefreshModels,
  onToggleMcp,
  onSelectProject,
  onSelectSkill,
  onSelectModel,
  onResetAll,
  isOpen,
  onToggleOpen,
  direction = 'up',
  compact = false,
  disabled = false,
}: ConfigMenuProps) {
  const [tab, setTab] = useState<Tab>('root');
  // Reset to root when the menu closes.
  useEffect(() => {
    if (!isOpen) setTab('root');
  }, [isOpen]);

  const activeMcpCount = mcpOptions.filter((m) => m.active).length;
  const modelIsNonDefault = activeModel !== null && activeModel !== defaultModel;
  const effectiveModelId = activeModel ?? defaultModel;
  const totalActive =
    (activeProject ? 1 : 0) +
    (modelIsNonDefault ? 1 : 0) +
    (activeSkill ? 1 : 0) +
    activeMcpCount;
  const hasAnyActive = totalActive > 0;

  const activeProjectEntry = projects.find((p) =>
    p.project_id ? `managed:${p.project_id}` === activeProject : p.name === activeProject,
  );
  const projectLabel =
    activeProjectEntry?.name ?? (activeProject ? activeProject : 'Temporary directory');
  const modelLabel = activeModel ? shortModel(activeModel) : 'Default';
  const skillLabel = activeSkill ? `/${activeSkill}` : 'None';
  const mcpLabel = activeMcpCount > 0 ? `${activeMcpCount} active` : 'None';

  // Project groupings
  const configProjects = projects.filter((p) => p.source !== 'managed');
  const managedProjects = projects.filter((p) => p.source === 'managed');

  // Model ordering — unsupported (whisper/embedding) sink to the bottom.
  const orderedModels = useMemo(
    () =>
      [...models].sort((a, b) => {
        const aUn = isUnsupportedModel(a) ? 1 : 0;
        const bUn = isUnsupportedModel(b) ? 1 : 0;
        return aUn - bUn;
      }),
    [models],
  );

  // MCP scope groups (worst-status-wins indicator already computed by caller)
  const pluginOptions = mcpOptions.filter((o) => o.scope === 'plugin');
  const globalOptions = mcpOptions.filter((o) => o.scope !== 'project' && o.scope !== 'plugin');
  const projectOptions = mcpOptions.filter((o) => o.scope === 'project');
  const mcpSections: { label: string; items: McpOption[] }[] = [
    { label: 'Global', items: globalOptions },
    { label: 'Plugin', items: pluginOptions },
    { label: 'Project', items: projectOptions },
  ].filter((s) => s.items.length > 0);

  return (
    <Popover
      open={disabled ? false : isOpen}
      onOpenChange={(next) => {
        if (disabled) return;
        if (next !== isOpen) onToggleOpen();
      }}
    >
      <PopoverTrigger asChild>
        <button
          aria-label={disabled ? 'Session config (locked while running)' : 'Configure session'}
          disabled={disabled}
          title={disabled ? 'Locked while the agent is running' : undefined}
          className={`flex items-center gap-1.5 ${compact ? 'p-1.5' : 'px-2.5 py-1.5'} rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${hasAnyActive ? 'text-[hsl(var(--foreground))]' : ''} disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-[hsl(var(--muted-foreground))]`}
        >
          <Sliders className={`w-3.5 h-3.5 ${hasAnyActive ? '' : 'opacity-50'}`} />
          {compact ? (
            <span className="text-[10px] font-medium truncate max-w-[80px]">{projectLabel}</span>
          ) : (
            <>
              <span className="truncate max-w-[160px]">{projectLabel}</span>
              {isOpen ? (
                <ChevronUp className="w-3 h-3 opacity-50" />
              ) : (
                <ChevronDown className="w-3 h-3 opacity-50" />
              )}
            </>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        side={direction === 'up' ? 'top' : 'bottom'}
        align="start"
        className="w-80 max-h-[420px] p-0 flex flex-col overflow-hidden"
      >
        <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)} className="flex flex-col flex-1 min-h-0">
          <TabsList className="grid grid-cols-5 m-2 mb-0 h-8">
            <TabsTrigger value="root" className="text-[10px] px-1">Root</TabsTrigger>
            <TabsTrigger value="project" className="text-[10px] px-1">Project</TabsTrigger>
            <TabsTrigger value="model" className="text-[10px] px-1">Model</TabsTrigger>
            <TabsTrigger value="skills" className="text-[10px] px-1">Skills</TabsTrigger>
            <TabsTrigger value="mcps" className="text-[10px] px-1">MCPs</TabsTrigger>
          </TabsList>

          {/* Root: summary rows that drill into other tabs */}
          <TabsContent value="root" className="m-0 flex-1 min-h-0 overflow-y-auto">
            <div className="py-1">
              <CategoryRow
                icon={<FolderOpen className={`w-3.5 h-3.5 ${activeProject ? 'text-blue-500' : 'opacity-50'}`} />}
                label="Project"
                value={projectLabel}
                emphasized={!!activeProject}
                onClick={() => setTab('project')}
              />
              <CategoryRow
                icon={
                  effectiveModelId && getProviderIcon(effectiveModelId) ? (
                    <ModelBrandIcon modelId={effectiveModelId} size={14} />
                  ) : (
                    <Cpu className={`w-3.5 h-3.5 ${modelIsNonDefault ? 'text-purple-500' : 'opacity-50'}`} />
                  )
                }
                label="Model"
                value={modelLabel}
                emphasized={modelIsNonDefault}
                onClick={() => setTab('model')}
              />
              <CategoryRow
                icon={<Zap className={`w-3.5 h-3.5 ${activeSkill ? 'text-amber-500' : 'opacity-50'}`} />}
                label="Skills"
                value={skillLabel}
                emphasized={!!activeSkill}
                onClick={() => setTab('skills')}
              />
              <CategoryRow
                icon={<Blocks className={`w-3.5 h-3.5 ${activeMcpCount > 0 ? 'text-emerald-500' : 'opacity-50'}`} />}
                label="Integrations"
                value={mcpLabel}
                emphasized={activeMcpCount > 0}
                onClick={() => setTab('mcps')}
              />
            </div>
            {hasAnyActive && (
              <>
                <div className="h-px bg-[hsl(var(--border))] mx-2 my-1" />
                <button
                  onClick={onResetAll}
                  type="button"
                  className="w-full text-left px-3 py-2 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] flex items-center gap-2 transition-colors"
                >
                  <RotateCcw className="w-3.5 h-3.5" />
                  <span>Reset to defaults</span>
                </button>
              </>
            )}
          </TabsContent>

          {/* Project tab */}
          <TabsContent value="project" className="m-0 flex-1 min-h-0 flex flex-col">
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter projects..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshProjects} label="Refresh projects" />
              </div>
              <ErrorBanner error={projectsError} />
              <CommandList className="max-h-[280px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {projectsLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                <CommandItem
                  value="__temp__ Temporary directory"
                  onSelect={() => { onSelectProject(null); setTab('root'); }}
                  className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${!activeProject ? 'font-medium' : ''}`}
                >
                  Temporary directory
                </CommandItem>
                {configProjects.length > 0 && (
                  <CommandGroup heading="Configured" className={COMMAND_GROUP_CLS}>
                    {configProjects.map((project) => {
                      const key = projectKey(project);
                      return (
                        <CommandItem
                          key={key}
                          value={`${project.name} ${project.description ?? ''}`}
                          onSelect={() => { onSelectProject(key); setTab('root'); }}
                          className={`${COMMAND_ITEM_TWO_LINE_CLS} ${activeProject === key ? 'font-medium' : ''}`}
                        >
                          <span>{project.name}</span>
                          {project.description && (
                            <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                              {project.description}
                            </span>
                          )}
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )}
                {managedProjects.length > 0 && (
                  <CommandGroup heading="Managed" className={COMMAND_GROUP_CLS}>
                    {managedProjects.map((project) => {
                      const key = projectKey(project);
                      return (
                        <CommandItem
                          key={key}
                          value={`${project.name} ${project.description ?? ''}`}
                          onSelect={() => { onSelectProject(key); setTab('root'); }}
                          className={`${COMMAND_ITEM_TWO_LINE_CLS} ${activeProject === key ? 'font-medium' : ''}`}
                        >
                          <span>{project.name}</span>
                          {project.description && (
                            <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                              {project.description}
                            </span>
                          )}
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )}
              </CommandList>
            </Command>
          </TabsContent>

          {/* Model tab */}
          <TabsContent value="model" className="m-0 flex-1 min-h-0 flex flex-col">
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter models..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshModels} label="Refresh models" />
              </div>
              <ErrorBanner error={modelsError} />
              <CommandList className="max-h-[280px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {modelsLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                <CommandItem
                  value="__default__ Default"
                  onSelect={() => { onSelectModel(null); setTab('root'); }}
                  className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${!activeModel ? 'font-medium' : ''}`}
                >
                  Default
                </CommandItem>
                {orderedModels.map((model) => {
                  const unsupported = isUnsupportedModel(model);
                  const isActive = activeModel === model;
                  return (
                    <CommandItem
                      key={model}
                      value={model}
                      onSelect={() => { onSelectModel(model); setTab('root'); }}
                      title={unsupported ? 'Not supported for chat or agents' : undefined}
                      className={`${COMMAND_ITEM_TWO_LINE_CLS} ${unsupported ? 'text-[hsl(var(--muted-foreground))]' : ''} ${isActive ? 'font-medium' : ''}`}
                    >
                      <span className="flex items-center gap-1.5">
                        <ModelBrandIcon modelId={model} size={14} />
                        {unsupported && (
                          <span role="img" aria-label="Not supported for chat" className="text-amber-500">
                            ⚠️
                          </span>
                        )}
                        <span>{shortModel(model)}</span>
                      </span>
                      {model.includes('/') && (
                        <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                          {model}
                        </span>
                      )}
                    </CommandItem>
                  );
                })}
              </CommandList>
            </Command>
          </TabsContent>

          {/* Skills tab */}
          <TabsContent value="skills" className="m-0 flex-1 min-h-0 flex flex-col">
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter skills..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshSkills} label="Refresh skills" />
              </div>
              <ErrorBanner error={skillsError} />
              <CommandList className="max-h-[280px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {skillsLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                <CommandItem
                  value="__none__ None"
                  onSelect={() => { onSelectSkill(null); setTab('root'); }}
                  className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${!activeSkill ? 'font-medium' : ''}`}
                >
                  None
                </CommandItem>
                {skills.map((skill) => (
                  <CommandItem
                    key={skill.name}
                    value={`${skill.name} ${skill.description ?? ''}`}
                    onSelect={() => { onSelectSkill(skill.name); setTab('root'); }}
                    className={`${COMMAND_ITEM_TWO_LINE_CLS} ${activeSkill === skill.name ? 'font-medium' : ''}`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span>/{skill.name}</span>
                      <span className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded">
                        {skill.source}
                      </span>
                    </span>
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                      {skill.description}
                    </span>
                  </CommandItem>
                ))}
              </CommandList>
            </Command>
          </TabsContent>

          {/* Integrations (MCPs) tab — multi-select, so do NOT auto-return to root on toggle. */}
          <TabsContent value="mcps" className="m-0 flex-1 min-h-0 flex flex-col">
            <Command className="flex-1 min-h-0">
              <div className="flex items-center justify-between pr-2">
                <CommandInput placeholder="Filter integrations..." className={COMMAND_INPUT_CLS} />
                <RefreshIcon onRefresh={onRefreshMcp} label="Refresh MCP tools" />
              </div>
              <ErrorBanner error={mcpError} />
              <CommandList className="max-h-[280px]">
                <CommandEmpty className={COMMAND_EMPTY_CLS}>
                  {mcpLoading ? 'Loading...' : 'No matches.'}
                </CommandEmpty>
                {mcpSections.length === 0 && !mcpLoading && (
                  <div className="px-4 py-3 text-xs text-[hsl(var(--muted-foreground))] text-center">
                    No MCP servers available.
                  </div>
                )}
                {mcpSections.map((section) => (
                  <CommandGroup key={section.label} heading={section.label} className={COMMAND_GROUP_CLS}>
                    {section.items.map((mcp) => (
                      <CommandItem
                        key={mcp.id}
                        value={mcp.name}
                        disabled={mcp.status === 'error'}
                        onSelect={() => onToggleMcp(mcp.id)}
                        // MCP rows are single-line (name + count badge + status dot on the right)
                        className={`${COMMAND_ITEM_SINGLE_LINE_CLS} justify-between ${mcp.enabled ? '' : 'text-[hsl(var(--muted-foreground))]'}`}
                      >
                        <span className="flex items-center gap-2 truncate flex-1">
                          <span className="truncate">{mcp.name}</span>
                          {typeof mcp.count === 'number' && (
                            <span className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded">
                              {mcp.count}
                            </span>
                          )}
                        </span>
                        <StatusDot status={mcp.status} active={mcp.active} />
                      </CommandItem>
                    ))}
                  </CommandGroup>
                ))}
              </CommandList>
            </Command>
          </TabsContent>
        </Tabs>
      </PopoverContent>
    </Popover>
  );
}

function CategoryRow({
  icon,
  label,
  value,
  emphasized,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  emphasized: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      type="button"
      className="w-full flex items-center gap-2 px-3 py-2 text-xs text-left hover:bg-[hsl(var(--accent))] transition-colors"
    >
      <span className="shrink-0">{icon}</span>
      <span className="text-[hsl(var(--foreground))] w-20 shrink-0">{label}</span>
      <span
        className={`flex-1 truncate ${emphasized ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
      >
        {value}
      </span>
    </button>
  );
}
