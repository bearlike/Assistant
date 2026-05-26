import { useEffect, useMemo, useState } from 'react';
import {
  Blocks,
  ChevronDown,
  ChevronUp,
  Circle,
  Cpu,
  FolderOpen,
  GitBranch,
  GitFork,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Sliders,
  Trash2,
  Zap,
} from 'lucide-react';
import { ProjectSummary, SkillSummary } from '../api/client';
import { CreateWorktreeInput, WorktreeSummary } from '../types';
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
  // Branch / worktree (optional — if ``gitRepo`` is false the tabs hide)
  gitRepo?: boolean;
  branches?: string[];
  currentBranch?: string | null;
  /**
   * Branches already checked out by the parent repo or another worktree —
   * ``git worktree add`` will refuse them. Surfaced so the "reuse existing
   * branch" path can disable them up-front instead of failing on POST.
   */
  branchesInUse?: string[];
  worktrees?: WorktreeSummary[];
  activeBranch?: string | null;
  activeWorktree?: string | null;
  gitLoading?: boolean;
  gitMutating?: boolean;
  gitError?: string | null;
  /** Read-only mode for in-session composer — picks are surfaced for
   * context but disabled. Worktrees can never be deleted from this surface. */
  gitReadOnly?: boolean;
  onSelectBranch?: (branch: string | null) => void;
  onSelectWorktree?: (worktreeId: string | null) => void;
  /**
   * Create a worktree. The structured payload makes the two modes
   * explicit: ``{branch}`` reuses an existing branch; ``{branch, base}``
   * creates a fresh branch from <base> via ``git worktree add -b``.
   */
  onCreateWorktree?: (input: CreateWorktreeInput) => void;
  onDeleteWorktree?: (worktreeId: string) => void;
  onRefreshGit?: () => void;
  // Popover control
  isOpen: boolean;
  onToggleOpen: () => void;
  direction?: 'up' | 'down';
  compact?: boolean;
  /** When true, the trigger button is non-interactive (used while a session is
   * running — selections are locked while the agent steers the existing context). */
  disabled?: boolean;
};

type Tab = 'root' | 'project' | 'branch' | 'worktree' | 'model' | 'skills' | 'mcps';

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
  gitRepo = false,
  branches = [],
  currentBranch = null,
  branchesInUse = [],
  worktrees = [],
  activeBranch = null,
  activeWorktree = null,
  gitLoading = false,
  gitMutating = false,
  gitError = null,
  gitReadOnly = false,
  onSelectBranch,
  onSelectWorktree,
  onCreateWorktree,
  onDeleteWorktree,
  onRefreshGit,
  isOpen,
  onToggleOpen,
  direction = 'up',
  compact = false,
  disabled = false,
}: ConfigMenuProps) {
  const [tab, setTab] = useState<Tab>('root');
  const showGitTabs = gitRepo;

  // Reset to root when the menu closes.
  useEffect(() => {
    if (!isOpen) setTab('root');
  }, [isOpen]);

  // Don't leave the user stranded on a tab whose surface just disappeared
  // (e.g. they switched from a git project to "Temporary directory").
  useEffect(() => {
    if (!showGitTabs && (tab === 'branch' || tab === 'worktree')) {
      setTab('root');
    }
  }, [showGitTabs, tab]);

  const activeMcpCount = mcpOptions.filter((m) => m.active).length;
  const modelIsNonDefault = activeModel !== null && activeModel !== defaultModel;
  const effectiveModelId = activeModel ?? defaultModel;
  const branchIsNonDefault =
    showGitTabs &&
    activeBranch !== null &&
    currentBranch !== null &&
    activeBranch !== currentBranch;
  const totalActive =
    (activeProject ? 1 : 0) +
    (modelIsNonDefault ? 1 : 0) +
    (activeSkill ? 1 : 0) +
    activeMcpCount +
    (branchIsNonDefault ? 1 : 0) +
    (activeWorktree ? 1 : 0);
  const hasAnyActive = totalActive > 0;

  const activeProjectEntry = projects.find((p) =>
    p.project_id ? `managed:${p.project_id}` === activeProject : p.name === activeProject,
  );
  const projectLabel =
    activeProjectEntry?.name ?? (activeProject ? activeProject : 'Temporary directory');
  const modelLabel = activeModel ? shortModel(activeModel) : 'Default';
  const skillLabel = activeSkill ? `/${activeSkill}` : 'None';
  const mcpLabel = activeMcpCount > 0 ? `${activeMcpCount} active` : 'None';
  const branchLabel = (() => {
    if (!showGitTabs) return 'Not a git repo';
    if (activeBranch) return activeBranch;
    if (currentBranch) return currentBranch;
    return 'Detached HEAD';
  })();
  // Match by project_id only when it is non-null on both sides — user-
  // created worktrees expose ``project_id: null`` and would otherwise
  // collide with an unset ``activeWorktree`` and look "selected".
  const activeWorktreeEntry =
    activeWorktree !== null
      ? worktrees.find((w) => w.project_id === activeWorktree)
      : undefined;
  const worktreeLabel = (() => {
    if (!showGitTabs) return '—';
    if (activeWorktreeEntry) return activeWorktreeEntry.branch;
    return 'Parent repo';
  })();
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
          <TabsList
            className={`grid ${showGitTabs ? 'grid-cols-7' : 'grid-cols-5'} m-2 mb-0 h-8`}
          >
            <TabsTrigger value="root" className="text-[10px] px-1">Root</TabsTrigger>
            <TabsTrigger value="project" className="text-[10px] px-1">Project</TabsTrigger>
            {showGitTabs && (
              <TabsTrigger value="branch" className="text-[10px] px-1">Branch</TabsTrigger>
            )}
            {showGitTabs && (
              <TabsTrigger value="worktree" className="text-[10px] px-1">Worktree</TabsTrigger>
            )}
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
              {showGitTabs && (
                <CategoryRow
                  icon={
                    <GitBranch
                      className={`w-3.5 h-3.5 ${branchIsNonDefault ? 'text-sky-500' : 'opacity-50'}`}
                    />
                  }
                  label="Branch"
                  value={branchLabel}
                  emphasized={branchIsNonDefault}
                  onClick={() => setTab('branch')}
                />
              )}
              {showGitTabs && (
                <CategoryRow
                  icon={
                    <GitFork
                      className={`w-3.5 h-3.5 ${activeWorktree ? 'text-violet-500' : 'opacity-50'}`}
                    />
                  }
                  label="Worktree"
                  value={worktreeLabel}
                  emphasized={!!activeWorktree}
                  onClick={() => setTab('worktree')}
                />
              )}
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

          {/* Branch tab — visible only when the active project is a git repo. */}
          {showGitTabs && (
            <TabsContent value="branch" className="m-0 flex-1 min-h-0 flex flex-col">
              <Command className="flex-1 min-h-0">
                <div className="flex items-center justify-between pr-2">
                  <CommandInput placeholder="Filter branches..." className={COMMAND_INPUT_CLS} />
                  <RefreshIcon onRefresh={onRefreshGit} label="Refresh git state" />
                </div>
                <ErrorBanner error={gitError} />
                <CommandList className="max-h-[280px]">
                  <CommandEmpty className={COMMAND_EMPTY_CLS}>
                    {gitLoading ? 'Loading...' : 'No matches.'}
                  </CommandEmpty>
                  {currentBranch && (
                    <CommandItem
                      value={`__current__ ${currentBranch}`}
                      onSelect={() => {
                        if (gitReadOnly) return;
                        onSelectBranch?.(currentBranch);
                        // Picking the current branch implies "no worktree"
                        // — back out of any active worktree selection so the
                        // session runs against the parent repo.
                        onSelectWorktree?.(null);
                        setTab('root');
                      }}
                      disabled={gitReadOnly}
                      className={`${COMMAND_ITEM_TWO_LINE_CLS} ${activeBranch === currentBranch || (!activeBranch && !branchIsNonDefault) ? 'font-medium' : ''}`}
                    >
                      <span className="flex items-center gap-2">
                        <GitBranch className="w-3 h-3" />
                        <span className="font-mono">{currentBranch}</span>
                        <span className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded">
                          current
                        </span>
                      </span>
                    </CommandItem>
                  )}
                  <CommandGroup heading="Branches" className={COMMAND_GROUP_CLS}>
                    {branches
                      .filter((b) => b !== currentBranch)
                      .map((branch) => {
                        const matchedWorktree = worktrees.find((w) => w.branch === branch);
                        const isActive = activeBranch === branch;
                        return (
                          <CommandItem
                            key={branch}
                            value={branch}
                            onSelect={() => {
                              if (gitReadOnly) return;
                              onSelectBranch?.(branch);
                              // If the picked branch already has a managed
                              // worktree, lock onto it so the session uses
                              // that directory instead of the parent.
                              if (matchedWorktree && matchedWorktree.managed) {
                                onSelectWorktree?.(matchedWorktree.project_id);
                              } else {
                                onSelectWorktree?.(null);
                              }
                              setTab('root');
                            }}
                            disabled={gitReadOnly}
                            className={`${COMMAND_ITEM_TWO_LINE_CLS} ${isActive ? 'font-medium' : ''}`}
                          >
                            <span className="flex items-center gap-2 truncate">
                              <GitBranch className="w-3 h-3 shrink-0" />
                              <span className="font-mono truncate">{branch}</span>
                            </span>
                            {matchedWorktree && (
                              <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5 flex items-center gap-1">
                                <GitFork className="w-3 h-3" />
                                {matchedWorktree.managed ? 'Has managed worktree' : 'Has user worktree'}
                              </span>
                            )}
                          </CommandItem>
                        );
                      })}
                  </CommandGroup>
                </CommandList>
              </Command>
            </TabsContent>
          )}

          {/* Worktree tab — visible only when the active project is a git repo. */}
          {showGitTabs && (
            <TabsContent value="worktree" className="m-0 flex-1 min-h-0 flex flex-col">
              <Command className="flex-1 min-h-0">
                <div className="flex items-center justify-between pr-2">
                  <CommandInput placeholder="Filter worktrees..." className={COMMAND_INPUT_CLS} />
                  <RefreshIcon onRefresh={onRefreshGit} label="Refresh worktrees" />
                </div>
                <ErrorBanner error={gitError} />
                <CommandList className="max-h-[280px]">
                  <CommandEmpty className={COMMAND_EMPTY_CLS}>
                    {gitLoading ? 'Loading...' : 'No matches.'}
                  </CommandEmpty>
                  <CommandItem
                    value="__parent__ Parent repo"
                    onSelect={() => {
                      if (gitReadOnly) return;
                      onSelectWorktree?.(null);
                      // Snap branch back to parent's current to keep the two
                      // selectors in sync — picking "no worktree" is the
                      // same gesture as "use whatever's checked out".
                      onSelectBranch?.(currentBranch ?? null);
                      setTab('root');
                    }}
                    disabled={gitReadOnly}
                    className={`${COMMAND_ITEM_TWO_LINE_CLS} ${!activeWorktree ? 'font-medium' : ''}`}
                  >
                    <span className="flex items-center gap-2">
                      <FolderOpen className="w-3 h-3" />
                      <span>Parent repo</span>
                    </span>
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                      {currentBranch
                        ? `Run against ${currentBranch} in the parent working tree`
                        : 'Run against the parent working tree'}
                    </span>
                  </CommandItem>
                  {worktrees.length > 0 && (
                    <CommandGroup heading="Worktrees" className={COMMAND_GROUP_CLS}>
                      {worktrees.map((wt) => {
                        const id = wt.project_id ?? `unmanaged:${wt.branch}`;
                        const isActive =
                          activeWorktree !== null &&
                          wt.project_id !== null &&
                          activeWorktree === wt.project_id;
                        const selectable = !gitReadOnly && wt.managed;
                        return (
                          <CommandItem
                            key={id}
                            value={`${wt.branch} ${wt.path}`}
                            onSelect={() => {
                              if (!selectable || !wt.project_id) return;
                              onSelectBranch?.(wt.branch);
                              onSelectWorktree?.(wt.project_id);
                              setTab('root');
                            }}
                            disabled={!selectable}
                            className={`${COMMAND_ITEM_TWO_LINE_CLS} ${isActive ? 'font-medium' : ''}`}
                          >
                            <span className="flex items-center gap-2 truncate">
                              <GitFork className="w-3 h-3 shrink-0" />
                              <span className="font-mono truncate">{wt.branch}</span>
                              <span
                                className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded shrink-0"
                                title={wt.managed ? 'Created by Mewbo' : 'Existing git worktree on disk'}
                              >
                                {wt.managed ? 'managed' : 'user'}
                              </span>
                              {wt.clean === false && (
                                <span
                                  className="text-amber-400 text-[10px] shrink-0"
                                  title="Uncommitted changes or unpushed commits"
                                >
                                  ●
                                </span>
                              )}
                              {!gitReadOnly && wt.managed && wt.project_id && onDeleteWorktree && (
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    onDeleteWorktree(wt.project_id as string);
                                  }}
                                  aria-label={`Delete worktree ${wt.branch}`}
                                  className="ml-auto p-1 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                                >
                                  <Trash2 className="w-3 h-3" />
                                </button>
                              )}
                            </span>
                            <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                              {wt.path}
                              {!wt.managed && ' (use git CLI to remove)'}
                            </span>
                          </CommandItem>
                        );
                      })}
                    </CommandGroup>
                  )}
                  {!gitReadOnly && onCreateWorktree && branches.length > 0 && (
                    <CommandGroup heading="Create new worktree" className={COMMAND_GROUP_CLS}>
                      <NewWorktreeForm
                        branches={branches}
                        currentBranch={currentBranch}
                        branchesInUse={branchesInUse}
                        existingWorktreeBranches={worktrees.map((w) => w.branch)}
                        busy={gitMutating}
                        onSubmit={onCreateWorktree}
                      />
                    </CommandGroup>
                  )}
                </CommandList>
              </Command>
            </TabsContent>
          )}

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

// ---------------------------------------------------------------------------
// NewWorktreeForm — explicit "create from base" flow.
//
// Two visible inputs:
//   1. Base branch — any existing branch (including ones currently checked
//      out elsewhere; that's exactly the case ``-b`` was designed for).
//   2. New branch name — pre-filled with ``mewbo/<base-slug>-<short-id>`` so
//      the result is always unique and obviously session-owned. Editable so
//      the user keeps full control and can pick a non-mewbo name (e.g. when
//      they intend the branch to outlive the worktree).
//
// A "use existing branch" toggle exposes the legacy single-branch flow for
// the rarer case where the user has already manually created a free branch
// they want a worktree on. KISS: same form, just disables the "new name"
// field and submits without ``base``.
// ---------------------------------------------------------------------------

const MEWBO_BRANCH_PREFIX = 'mewbo/';

/** Slugify a branch name into a directory-safe token. Mirrors the Python
 * ``slugify_branch`` helper closely enough for default-name generation. */
function slugifyBranchClient(branch: string): string {
  return branch
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-._]+|[-._]+$/g, '')
    || 'branch';
}

/** Browser-compatible 6-hex token. ``crypto.randomUUID`` is widely
 * available; we slice it for compactness. Falls back to ``Math.random`` so
 * tests and ancient environments don't crash. */
function shortId(): string {
  const c = (globalThis as unknown as { crypto?: Crypto }).crypto;
  if (c?.getRandomValues) {
    const bytes = new Uint8Array(3);
    c.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }
  return Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0');
}

function defaultMewboBranchName(base: string): string {
  return `${MEWBO_BRANCH_PREFIX}${slugifyBranchClient(base)}-${shortId()}`;
}

type NewWorktreeFormProps = {
  branches: string[];
  currentBranch: string | null;
  branchesInUse: string[];
  /** Branches that already back a managed worktree — disabled in "reuse" mode. */
  existingWorktreeBranches: string[];
  busy: boolean;
  onSubmit: (input: CreateWorktreeInput) => void;
};

function NewWorktreeForm({
  branches,
  currentBranch,
  branchesInUse,
  existingWorktreeBranches,
  busy,
  onSubmit,
}: NewWorktreeFormProps) {
  // Mode toggle. Default = "new" (the recommended workflow).
  const [mode, setMode] = useState<'new' | 'existing'>('new');

  // Base branch (used in both modes — in "existing" mode it IS the branch).
  const initialBase = currentBranch ?? branches[0] ?? '';
  const [base, setBase] = useState<string>(initialBase);

  // Name of the branch to create (only used in "new" mode).
  const [newBranch, setNewBranch] = useState<string>(() =>
    initialBase ? defaultMewboBranchName(initialBase) : '',
  );

  // Re-prefill the new-branch name whenever the base changes — the user
  // can still override afterwards. We don't overwrite if they've already
  // edited away from a mewbo/ default (heuristic: starts with the prefix).
  useEffect(() => {
    setNewBranch((cur) => {
      if (!base) return cur;
      if (!cur || cur.startsWith(MEWBO_BRANCH_PREFIX)) {
        return defaultMewboBranchName(base);
      }
      return cur;
    });
  }, [base]);

  const inUse = new Set(branchesInUse);
  const existingWtSet = new Set(existingWorktreeBranches);

  const canSubmit = (() => {
    if (busy) return false;
    if (!base) return false;
    if (mode === 'new') {
      const trimmed = newBranch.trim();
      if (!trimmed) return false;
      // The new branch must not already exist (git would refuse) — cheap
      // local check against the visible branch list.
      if (branches.includes(trimmed)) return false;
      return true;
    }
    // "existing" mode: the chosen branch must be free (not currently
    // checked out anywhere) and not already a managed worktree branch.
    if (inUse.has(base)) return false;
    if (existingWtSet.has(base)) return false;
    return true;
  })();

  const submit = () => {
    if (!canSubmit) return;
    if (mode === 'new') {
      onSubmit({ branch: newBranch.trim(), base });
    } else {
      onSubmit({ branch: base });
    }
  };

  // Tailwind shorthand reused for both inputs.
  const inputCls =
    'w-full text-xs rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1 font-mono';

  return (
    <div className="px-3 py-2 flex flex-col gap-2 text-xs">
      {/* Mode toggle: explicit + transparent, no surprise behaviour. */}
      <div className="flex items-center gap-1 text-[10px] text-[hsl(var(--muted-foreground))]">
        <button
          type="button"
          onClick={() => setMode('new')}
          className={`px-2 py-0.5 rounded ${
            mode === 'new'
              ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] font-medium'
              : 'hover:bg-[hsl(var(--accent))]'
          }`}
        >
          New branch from base
        </button>
        <button
          type="button"
          onClick={() => setMode('existing')}
          className={`px-2 py-0.5 rounded ${
            mode === 'existing'
              ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] font-medium'
              : 'hover:bg-[hsl(var(--accent))]'
          }`}
        >
          Use existing branch
        </button>
      </div>

      {/* Base branch picker — in "existing" mode this IS the branch. */}
      <label className="flex flex-col gap-1">
        <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          {mode === 'new' ? 'Base branch' : 'Branch'}
        </span>
        <select
          value={base}
          onChange={(e) => setBase(e.target.value)}
          disabled={busy || branches.length === 0}
          className={inputCls}
        >
          {branches.map((b) => {
            // In "existing" mode disable branches that can't back a worktree.
            const disabled =
              mode === 'existing' && (inUse.has(b) || existingWtSet.has(b));
            const suffix = inUse.has(b)
              ? ' (in use)'
              : existingWtSet.has(b)
                ? ' (worktree exists)'
                : b === currentBranch
                  ? ' (current)'
                  : '';
            return (
              <option key={b} value={b} disabled={disabled}>
                {b}
                {suffix}
              </option>
            );
          })}
        </select>
      </label>

      {/* New branch name — only visible in "new" mode. */}
      {mode === 'new' && (
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
            New branch name
          </span>
          <div className="flex items-center gap-1">
            <input
              type="text"
              value={newBranch}
              onChange={(e) => setNewBranch(e.target.value)}
              placeholder="mewbo/feature-x-ab12cd"
              disabled={busy}
              className={inputCls}
            />
            <button
              type="button"
              onClick={() => setNewBranch(defaultMewboBranchName(base))}
              disabled={busy || !base}
              title="Generate a fresh mewbo/<base>-<id> name"
              className="shrink-0 px-1.5 py-1 rounded hover:bg-[hsl(var(--accent))] text-[hsl(var(--muted-foreground))]"
            >
              <RefreshCw className="w-3 h-3" />
            </button>
          </div>
          {newBranch && branches.includes(newBranch.trim()) && (
            <span className="text-[10px] text-amber-400">
              Branch already exists — pick a different name or switch to
              &ldquo;Use existing branch&rdquo;.
            </span>
          )}
        </label>
      )}

      {/* Transparency: spell out what the click is going to do. */}
      <p className="text-[10px] text-[hsl(var(--muted-foreground))] leading-snug">
        {mode === 'new' && base && newBranch.trim()
          ? `Will run: git worktree add -b ${newBranch.trim()} <path> ${base}`
          : mode === 'existing' && base
            ? `Will run: git worktree add <path> ${base}`
            : 'Pick a base branch to continue.'}
      </p>

      <button
        type="button"
        onClick={submit}
        disabled={!canSubmit}
        className="self-start inline-flex items-center gap-1.5 px-2 py-1 rounded border border-[hsl(var(--border))] bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] disabled:opacity-50"
      >
        {busy ? (
          <Loader2 className="w-3 h-3 animate-spin" />
        ) : (
          <Plus className="w-3 h-3" />
        )}
        Create worktree
      </button>
    </div>
  );
}
