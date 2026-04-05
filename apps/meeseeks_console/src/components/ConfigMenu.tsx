import { forwardRef, useEffect, useMemo, useState } from 'react';
import {
  Blocks,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
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
import { Popover } from './Popover';

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

type View = 'root' | 'project' | 'model' | 'skills' | 'mcps';

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
};

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

function StatusDot({ status, active }: { status: McpStatus; active: boolean }) {
  if (status === 'active' && !active) {
    return <Circle className="w-2 h-2 shrink-0 fill-[hsl(var(--muted))] text-[hsl(var(--border))]" />;
  }
  return <Circle className={`w-2 h-2 shrink-0 ${DOT_CLASSES[status]}`} />;
}

function RefreshButton({ onRefresh, label }: { onRefresh?: () => void; label: string }) {
  if (!onRefresh) return null;
  return (
    <button
      onClick={onRefresh}
      aria-label={label}
      className="p-0.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
    >
      <RefreshCw className="w-3 h-3" />
    </button>
  );
}

function CategoryHeader({
  title,
  onBack,
  onRefresh,
  refreshLabel,
}: {
  title: string;
  onBack: () => void;
  onRefresh?: () => void;
  refreshLabel: string;
}) {
  return (
    <div className="flex items-center justify-between px-2 pt-2 pb-1 border-b border-[hsl(var(--border))]">
      <button
        onClick={onBack}
        aria-label="Back to configure"
        className="flex items-center gap-1 text-[10px] font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] uppercase tracking-wider transition-colors px-1 py-0.5 rounded"
      >
        <ChevronLeft className="w-3 h-3" />
        <span>{title}</span>
      </button>
      <RefreshButton onRefresh={onRefresh} label={refreshLabel} />
    </div>
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

function EmptyState({ message }: { message: string }) {
  return (
    <div className="px-4 py-3 text-xs text-[hsl(var(--muted-foreground))] text-center">
      {message}
    </div>
  );
}

export const ConfigMenu = forwardRef<HTMLDivElement, ConfigMenuProps>(
  (
    {
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
    },
    ref,
  ) => {
    const [view, setView] = useState<View>('root');

    // Reset to root each time the menu closes
    useEffect(() => {
      if (!isOpen) {
        setView('root');
      }
    }, [isOpen]);

    const activeMcpCount = mcpOptions.filter((m) => m.active).length;
    const modelIsNonDefault = activeModel !== null && activeModel !== defaultModel;
    const effectiveModelId = activeModel ?? defaultModel;
    const totalActive =
      (activeProject ? 1 : 0) +
      (modelIsNonDefault ? 1 : 0) +
      (activeSkill ? 1 : 0) +
      activeMcpCount;

    // Inline summaries for root view rows
    const projectLabel = activeProject ?? 'Default';
    const modelLabel = activeModel ? shortModel(activeModel) : 'Default';
    const skillLabel = activeSkill ? `/${activeSkill}` : 'None';
    const mcpLabel = activeMcpCount > 0 ? `${activeMcpCount} active` : 'None';

    const hasAnyActive = totalActive > 0;

    return (
      <div className="relative" ref={ref}>
        <button
          onClick={onToggleOpen}
          aria-label="Configure session"
          className={`flex items-center gap-1.5 ${compact ? 'p-1.5' : 'px-2.5 py-1.5'} rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${hasAnyActive ? 'text-[hsl(var(--foreground))]' : ''}`}
        >
          <Sliders className={`w-3.5 h-3.5 ${hasAnyActive ? '' : 'opacity-50'}`} />
          {compact ? (
            hasAnyActive && <span className="text-[10px] font-medium">{totalActive}</span>
          ) : (
            <>
              <span>Configure</span>
              {hasAnyActive && (
                <span className="text-[10px] font-medium bg-[hsl(var(--muted))] text-[hsl(var(--foreground))] px-1.5 py-0.5 rounded">
                  {totalActive}
                </span>
              )}
              {isOpen ? (
                <ChevronUp className="w-3 h-3 opacity-50" />
              ) : (
                <ChevronDown className="w-3 h-3 opacity-50" />
              )}
            </>
          )}
        </button>

        {isOpen && (
          <Popover direction={direction} width="w-72" maxHeight="max-h-[360px]">
            {view === 'root' && (
              <RootView
                projectLabel={projectLabel}
                modelLabel={modelLabel}
                skillLabel={skillLabel}
                mcpLabel={mcpLabel}
                activeMcpCount={activeMcpCount}
                hasProject={!!activeProject}
                hasModel={modelIsNonDefault}
                hasSkill={!!activeSkill}
                hasAnyActive={hasAnyActive}
                activeModelId={effectiveModelId}
                onDrill={setView}
                onResetAll={onResetAll}
              />
            )}
            {view === 'project' && (
              <ProjectView
                projects={projects}
                activeProject={activeProject}
                loading={projectsLoading}
                error={projectsError}
                onRefresh={onRefreshProjects}
                onBack={() => setView('root')}
                onSelect={(name) => {
                  onSelectProject(name);
                  setView('root');
                }}
              />
            )}
            {view === 'model' && (
              <ModelView
                models={models}
                activeModel={activeModel}
                loading={modelsLoading}
                error={modelsError}
                onRefresh={onRefreshModels}
                onBack={() => setView('root')}
                onSelect={(name) => {
                  onSelectModel(name);
                  setView('root');
                }}
              />
            )}
            {view === 'skills' && (
              <SkillsView
                skills={skills}
                activeSkill={activeSkill}
                loading={skillsLoading}
                error={skillsError}
                onRefresh={onRefreshSkills}
                onBack={() => setView('root')}
                onSelect={(name) => {
                  onSelectSkill(name);
                  setView('root');
                }}
              />
            )}
            {view === 'mcps' && (
              <McpsView
                options={mcpOptions}
                loading={mcpLoading}
                error={mcpError}
                onRefresh={onRefreshMcp}
                onBack={() => setView('root')}
                onToggle={onToggleMcp}
              />
            )}
          </Popover>
        )}
      </div>
    );
  },
);
ConfigMenu.displayName = 'ConfigMenu';

// ---------- Root view ----------

function RootView({
  projectLabel,
  modelLabel,
  skillLabel,
  mcpLabel,
  activeMcpCount,
  hasProject,
  hasModel,
  hasSkill,
  hasAnyActive,
  activeModelId,
  onDrill,
  onResetAll,
}: {
  projectLabel: string;
  modelLabel: string;
  skillLabel: string;
  mcpLabel: string;
  activeMcpCount: number;
  hasProject: boolean;
  hasModel: boolean;
  hasSkill: boolean;
  hasAnyActive: boolean;
  activeModelId: string | null;
  onDrill: (view: View) => void;
  onResetAll: () => void;
}) {
  return (
    <>
      <div className="px-3 pt-2 pb-1 border-b border-[hsl(var(--border))]">
        <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
          Configure
        </span>
      </div>
      <div className="py-0.5">
        <CategoryRow
          icon={<FolderOpen className={`w-3.5 h-3.5 ${hasProject ? 'text-blue-500' : 'opacity-50'}`} />}
          label="Project"
          value={projectLabel}
          emphasized={hasProject}
          onClick={() => onDrill('project')}
        />
        <CategoryRow
          icon={
            activeModelId && getProviderIcon(activeModelId) ? (
              <ModelBrandIcon modelId={activeModelId} size={14} />
            ) : (
              <Cpu className={`w-3.5 h-3.5 ${hasModel ? 'text-purple-500' : 'opacity-50'}`} />
            )
          }
          label="Model"
          value={modelLabel}
          emphasized={hasModel}
          onClick={() => onDrill('model')}
        />
        <CategoryRow
          icon={<Zap className={`w-3.5 h-3.5 ${hasSkill ? 'text-amber-500' : 'opacity-50'}`} />}
          label="Skills"
          value={skillLabel}
          emphasized={hasSkill}
          onClick={() => onDrill('skills')}
        />
        <CategoryRow
          icon={<Blocks className={`w-3.5 h-3.5 ${activeMcpCount > 0 ? 'text-emerald-500' : 'opacity-50'}`} />}
          label="Integrations"
          value={mcpLabel}
          emphasized={activeMcpCount > 0}
          onClick={() => onDrill('mcps')}
        />
      </div>
      {hasAnyActive && (
        <>
          <div className="h-px bg-[hsl(var(--border))] mx-2 my-1" />
          <button
            onClick={onResetAll}
            className="w-full text-left px-3 py-2 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] flex items-center gap-2 transition-colors"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            <span>Reset to defaults</span>
          </button>
        </>
      )}
    </>
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
      className="w-full flex items-center gap-2 px-3 py-2 text-xs text-left hover:bg-[hsl(var(--accent))] transition-colors"
    >
      <span className="shrink-0">{icon}</span>
      <span className="text-[hsl(var(--foreground))] w-20 shrink-0">{label}</span>
      <span
        className={`flex-1 truncate ${emphasized ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
      >
        {value}
      </span>
      <ChevronRight className="w-3 h-3 text-[hsl(var(--muted-foreground))] shrink-0" />
    </button>
  );
}

// ---------- Category views ----------

function ProjectView({
  projects,
  activeProject,
  loading,
  error,
  onRefresh,
  onBack,
  onSelect,
}: {
  projects: ProjectSummary[];
  activeProject: string | null;
  loading: boolean;
  error: string | null;
  onRefresh?: () => void;
  onBack: () => void;
  onSelect: (name: string | null) => void;
}) {
  return (
    <>
      <CategoryHeader
        title="Project"
        onBack={onBack}
        onRefresh={onRefresh}
        refreshLabel="Refresh projects"
      />
      <ErrorBanner error={error} />
      {projects.length === 0 && !loading && <EmptyState message="No projects available." />}
      <div className="py-1">
        <button
          onClick={() => onSelect(null)}
          className={`w-full flex items-center justify-between px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${!activeProject ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
        >
          <span>Default (server CWD)</span>
        </button>
        {projects.map((project) => (
          <button
            key={project.name}
            onClick={() => onSelect(project.name)}
            className={`w-full flex flex-col items-start px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${activeProject === project.name ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--foreground))]'}`}
          >
            <span>{project.name}</span>
            {project.description && (
              <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                {project.description}
              </span>
            )}
          </button>
        ))}
      </div>
    </>
  );
}

function ModelView({
  models,
  activeModel,
  loading,
  error,
  onRefresh,
  onBack,
  onSelect,
}: {
  models: string[];
  activeModel: string | null;
  loading: boolean;
  error: string | null;
  onRefresh?: () => void;
  onBack: () => void;
  onSelect: (name: string | null) => void;
}) {
  // Push unsupported models (whisper/embedding) to the bottom; stable sort
  // preserves original order within each group.
  const orderedModels = useMemo(
    () =>
      [...models].sort((a, b) => {
        const aUn = isUnsupportedModel(a) ? 1 : 0;
        const bUn = isUnsupportedModel(b) ? 1 : 0;
        return aUn - bUn;
      }),
    [models],
  );
  return (
    <>
      <CategoryHeader
        title="Model"
        onBack={onBack}
        onRefresh={onRefresh}
        refreshLabel="Refresh models"
      />
      <ErrorBanner error={error} />
      {models.length === 0 && !loading && <EmptyState message="No models available." />}
      <div className="py-1">
        <button
          onClick={() => onSelect(null)}
          className={`w-full flex items-center justify-between px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${!activeModel ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
        >
          <span>Default</span>
        </button>
        {orderedModels.map((model) => {
          const unsupported = isUnsupportedModel(model);
          const isActive = activeModel === model;
          return (
            <button
              key={model}
              onClick={() => onSelect(model)}
              title={unsupported ? 'Not supported for chat or agents' : undefined}
              className={`w-full flex flex-col items-start px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${
                unsupported
                  ? 'text-[hsl(var(--muted-foreground))]'
                  : isActive
                    ? 'text-[hsl(var(--foreground))] font-medium'
                    : 'text-[hsl(var(--foreground))]'
              }`}
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
            </button>
          );
        })}
      </div>
    </>
  );
}

function SkillsView({
  skills,
  activeSkill,
  loading,
  error,
  onRefresh,
  onBack,
  onSelect,
}: {
  skills: SkillSummary[];
  activeSkill: string | null;
  loading: boolean;
  error: string | null;
  onRefresh?: () => void;
  onBack: () => void;
  onSelect: (name: string | null) => void;
}) {
  return (
    <>
      <CategoryHeader
        title="Skills"
        onBack={onBack}
        onRefresh={onRefresh}
        refreshLabel="Refresh skills"
      />
      <ErrorBanner error={error} />
      {skills.length === 0 && !loading && <EmptyState message="No skills available." />}
      <div className="py-1">
        <button
          onClick={() => onSelect(null)}
          className={`w-full flex items-center justify-between px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${!activeSkill ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
        >
          <span>None</span>
        </button>
        {skills.map((skill) => (
          <button
            key={skill.name}
            onClick={() => onSelect(skill.name)}
            className={`w-full flex flex-col items-start px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${activeSkill === skill.name ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--foreground))]'}`}
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
          </button>
        ))}
      </div>
    </>
  );
}

function McpsView({
  options,
  loading,
  error,
  onRefresh,
  onBack,
  onToggle,
}: {
  options: McpOption[];
  loading: boolean;
  error: string | null;
  onRefresh?: () => void;
  onBack: () => void;
  onToggle: (id: string) => void;
}) {
  const globalOptions = options.filter((o) => o.scope !== 'project');
  const projectOptions = options.filter((o) => o.scope === 'project');
  return (
    <>
      <CategoryHeader
        title="Integrations"
        onBack={onBack}
        onRefresh={onRefresh}
        refreshLabel="Refresh MCP tools"
      />
      <ErrorBanner error={error} />
      {options.length === 0 && !loading && <EmptyState message="No MCP servers available." />}
      {globalOptions.length > 0 && (
        <>
          <ScopeHeader label="Global" />
          <McpOptionList options={globalOptions} onToggle={onToggle} />
        </>
      )}
      {projectOptions.length > 0 && (
        <>
          {globalOptions.length > 0 && (
            <div className="h-px bg-[hsl(var(--border))] mx-2 my-1" />
          )}
          <ScopeHeader label="Project" />
          <McpOptionList options={projectOptions} onToggle={onToggle} />
        </>
      )}
    </>
  );
}

function ScopeHeader({ label }: { label: string }) {
  return (
    <div className="px-3 pt-2 pb-1">
      <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
        {label}
      </span>
    </div>
  );
}

function McpOptionList({
  options,
  onToggle,
}: {
  options: McpOption[];
  onToggle: (id: string) => void;
}) {
  return (
    <div className="py-0.5">
      {options.map((mcp) => (
        <button
          key={mcp.id}
          onClick={() => onToggle(mcp.id)}
          disabled={mcp.status === 'error'}
          className={`w-full flex items-center justify-between px-3 py-1.5 text-xs text-left transition-colors ${
            mcp.status === 'error'
              ? 'text-[hsl(var(--muted-foreground))] opacity-60 cursor-not-allowed'
              : mcp.enabled
                ? 'text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]'
                : 'text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))]'
          }`}
        >
          <span className="flex items-center gap-2 truncate">
            <span className="truncate">{mcp.name}</span>
            {typeof mcp.count === 'number' && (
              <span className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded">
                {mcp.count}
              </span>
            )}
          </span>
          <StatusDot status={mcp.status} active={mcp.active} />
        </button>
      ))}
    </div>
  );
}
