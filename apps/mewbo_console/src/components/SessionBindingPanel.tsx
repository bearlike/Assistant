/**
 * SessionBindingPanel — a read-only view of a session's durable purpose-binding.
 *
 * What once needed Mongo forensics (which surface created this session, on what
 * model ladder, under which tool ceiling and capabilities) reads at a glance
 * here. Mounted as the "Binding" tab on the WorkspacePanel; self-contained,
 * fetching its own `useSessionSpec(sessionId)` (TanStack dedupes by queryKey).
 *
 * Strictly read-only — enforcement lives on the server, this is honesty +
 * informed control. Sections hide when they carry nothing (the inspector
 * `PanelShell`/`parts` pattern), so a bare open chat shows only what it actually
 * binds. The one field whose lock is load-bearing — the tool ceiling — reads its
 * locked/open state from the server's fail-closed `editable` map, never a guess.
 */
import { Anchor, Lock, Unlock } from "lucide-react";

import { cn } from "../utils/cn";
import { ORIGIN_META } from "../utils/sessionOrigins";
import type { SessionSpecBinding, SessionSpecEditable } from "../types";
import { useSessionSpec } from "../hooks/useSessionSpec";
import { Badge } from "./agents";

// ── presentational atoms (the inspector `parts.tsx` vocabulary, local so the
//    panel doesn't import wiki-graph types) ─────────────────────────────────

/** A small uppercase muted section label. */
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1.5">
      {children}
    </div>
  );
}

/** A labelled block. Renders nothing when `hidden` — the hide-when-empty rule. */
function Section({
  title,
  children,
  hidden,
}: {
  title: React.ReactNode;
  children: React.ReactNode;
  hidden?: boolean;
}) {
  if (hidden) return null;
  return (
    <section>
      <SectionLabel>{title}</SectionLabel>
      <div className="space-y-1.5">{children}</div>
    </section>
  );
}

/**
 * One label → value row. Renders nothing when the value is absent (null /
 * undefined / empty string), so a section composed of `Field`s collapses to
 * nothing when the binding carries none of them. `mono` marks machine text a
 * reader might copy (ids, paths, model names).
 */
function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: string | number | null | undefined;
  mono?: boolean;
}) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-[hsl(var(--muted-foreground))] shrink-0">{label}</span>
      <span
        className={cn(
          "text-sm text-[hsl(var(--foreground))] text-right break-words min-w-0",
          mono && "font-mono text-xs",
        )}
      >
        {value}
      </span>
    </div>
  );
}

/** A monospace chip for one tool / capability id. */
function IdChip({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-md bg-[hsl(var(--muted))]/60 px-1.5 py-0.5 font-mono text-2xs text-[hsl(var(--foreground))]">
      {children}
    </span>
  );
}

// ── tool ceiling — the one load-bearing three-state read ────────────────────

/**
 * The MCP tool ceiling, three-state per the backend contract: `null` = open (no
 * ceiling), `[]` = a real ceiling granting no tool, a non-empty list = exactly
 * those. Whether it is LOCKED comes from the server's `editable` map, not a
 * guess — a field the server reports non-`true` is refused server-side, so it
 * reads as a locked ceiling rather than an editable list.
 */
function ToolCeiling({
  allowedTools,
  strictScope,
  editable,
}: {
  allowedTools: string[] | null;
  strictScope: boolean;
  editable: SessionSpecEditable;
}) {
  const locked = editable.allowed_tools !== true;

  return (
    <Section title="Tools">
      <div className="flex items-center gap-1.5">
        {allowedTools === null ? (
          <>
            <Unlock className="size-3.5 text-[hsl(var(--muted-foreground))]" aria-hidden />
            <span className="text-sm text-[hsl(var(--foreground))]">Open</span>
            <span className="text-xs text-[hsl(var(--muted-foreground))]">· no tool ceiling</span>
          </>
        ) : (
          <>
            {locked ? (
              <Lock className="size-3.5 text-[hsl(var(--primary-text))]" aria-hidden />
            ) : (
              <Unlock className="size-3.5 text-[hsl(var(--muted-foreground))]" aria-hidden />
            )}
            <span className="text-sm text-[hsl(var(--foreground))]">
              {locked ? "Locked" : "Ceiling"}
            </span>
            <span className="text-xs text-[hsl(var(--muted-foreground))]">
              ·{" "}
              {allowedTools.length === 0
                ? "no MCP tools granted"
                : `${allowedTools.length} tool${allowedTools.length === 1 ? "" : "s"}`}
            </span>
          </>
        )}
      </div>
      {allowedTools !== null && allowedTools.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {allowedTools.map((id) => (
            <IdChip key={id}>{id}</IdChip>
          ))}
        </div>
      )}
      {strictScope && (
        <div className="text-xs text-[hsl(var(--muted-foreground))]">
          Strict scope — the allowlist overrides built-in tools too.
        </div>
      )}
    </Section>
  );
}

// ── panel ───────────────────────────────────────────────────────────────────

export function SessionBindingPanel({ sessionId }: { sessionId: string }) {
  const { spec, editable, source, isLoading, error } = useSessionSpec(sessionId);

  // Calm empty states — chrome shows no error residue (the compact-design law).
  if (isLoading) {
    return <CalmMessage>Loading binding…</CalmMessage>;
  }
  if (error || !spec) {
    return <CalmMessage>Binding unavailable.</CalmMessage>;
  }

  return (
    <div className="h-full overflow-y-auto">
      <BindingHeader spec={spec} source={source} />
      <div className="px-4 py-4 space-y-4">
        <Section title="Purpose">
          <div className="flex items-center gap-2">
            <OriginChip origin={spec.origin} />
          </div>
          <Field label="Surface" value={spec.surface} />
        </Section>

        <Section
          title="Placement"
          hidden={!spec.project && !spec.slug && !spec.cwd}
        >
          <Field label="Project" value={spec.project} />
          <Field label="Slug" value={spec.slug} mono />
          <Field label="Working dir" value={spec.cwd} mono />
        </Section>

        <Section title="Model" hidden={!spec.model && !spec.fallback_models?.length}>
          <Field label="Model" value={spec.model} mono />
          {spec.fallback_models && spec.fallback_models.length > 0 && (
            <div>
              <div className="text-xs text-[hsl(var(--muted-foreground))] mb-1">Fallback ladder</div>
              <ol className="space-y-0.5">
                {spec.fallback_models.map((m, i) => (
                  <li key={m} className="flex items-center gap-2">
                    <span className="text-2xs tabular-nums text-[hsl(var(--muted-foreground))] w-4 text-right">
                      {i + 1}
                    </span>
                    <span className="font-mono text-xs text-[hsl(var(--foreground))] break-words min-w-0">
                      {m}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </Section>

        <ToolCeiling
          allowedTools={spec.allowed_tools}
          strictScope={spec.strict_tool_scope}
          editable={editable}
        />

        <Section
          title="Capabilities"
          hidden={!spec.capabilities?.length && !spec.skill_instructions_present}
        >
          {spec.capabilities && spec.capabilities.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {spec.capabilities.map((c) => (
                <IdChip key={c}>{c}</IdChip>
              ))}
            </div>
          )}
          {spec.skill_instructions_present && (
            <div className="text-xs text-[hsl(var(--muted-foreground))]">
              A skill playbook is bound to this session.
            </div>
          )}
        </Section>

        <Section
          title="Limits"
          hidden={spec.session_step_budget === null && !spec.mode}
        >
          <Field
            label="Step budget"
            value={spec.session_step_budget === null ? null : spec.session_step_budget}
          />
          {spec.mode && (
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-xs text-[hsl(var(--muted-foreground))] shrink-0">Mode</span>
              <span className="inline-flex items-center rounded-md bg-[hsl(var(--muted))]/60 px-1.5 py-0.5 text-2xs text-[hsl(var(--foreground))]">
                {spec.mode}
              </span>
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}

/** The header: what the session is bound to, and whether the binding is durable
 *  or reconstructed. `purpose_bound` reads as the prominent Locked/Open state. */
function BindingHeader({
  spec,
  source,
}: {
  spec: SessionSpecBinding;
  source: "spec" | "legacy_context" | null;
}) {
  return (
    <header className="px-4 py-3 border-b border-[hsl(var(--border))] space-y-2">
      <div className="flex items-center gap-2">
        <Anchor className="size-4 text-[hsl(var(--muted-foreground))]" aria-hidden />
        <span className="text-sm font-medium text-[hsl(var(--foreground))]">Session binding</span>
        {source === "legacy_context" && (
          <span className="text-2xs text-[hsl(var(--muted-foreground))]">reconstructed</span>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        {spec.purpose_bound ? (
          <>
            <Lock className="size-3.5 text-[hsl(var(--primary-text))]" aria-hidden />
            <span className="text-xs text-[hsl(var(--foreground))]">Purpose-bound</span>
            <span className="text-xs text-[hsl(var(--muted-foreground))]">
              · scope refuses overrides
            </span>
          </>
        ) : (
          <>
            <Unlock className="size-3.5 text-[hsl(var(--muted-foreground))]" aria-hidden />
            <span className="text-xs text-[hsl(var(--foreground))]">Open chat</span>
          </>
        )}
      </div>
    </header>
  );
}

/** The origin chip — reuses the shared `ORIGIN_META` registry + `Badge` so this
 *  and every other origin surface can't name the same provenance two ways. */
function OriginChip({ origin }: { origin: SessionSpecBinding["origin"] }) {
  const meta = ORIGIN_META[origin] ?? ORIGIN_META.user;
  const Icon = meta.icon;
  return (
    <Badge color={meta.color}>
      <span className="inline-flex items-center gap-1">
        <Icon className="size-3 shrink-0" aria-hidden />
        {meta.label}
      </span>
    </Badge>
  );
}

function CalmMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center p-6 text-sm text-[hsl(var(--muted-foreground))]">
      {children}
    </div>
  );
}
