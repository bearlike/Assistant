/**
 * ConfigureWizard step 1 — source: git-vs-catalog toggle, repo URL +
 * platform + token (git), or workspace name + documents (catalog).
 */

import { useMemo, useState } from "react";
import {
  BookOpen,
  ChevronDown,
  ChevronUp,
  Database,
  Eye,
  EyeOff,
  ExternalLink,
  GitFork,
  Info,
  KeyRound,
  ShieldCheck,
} from "lucide-react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { hostOf, matchCredential } from "../../../api/git";
import { useGitCredentials } from "../../../hooks/useGitCredentials";

import { CatalogDocsForm } from "../CatalogDocsForm";
import type { Platform } from "../api/types";
import { slugFromRepoUrl } from "../slug";
import { Field } from "./Field";
import { PlatformIcon } from "./PlatformIcon";
import { PlatformTile } from "./PlatformTile";
import { slugifyName, type WizardState } from "./wizardState";

export function StepSource({
  state,
  set,
  errors,
  platform,
  platforms,
  catalogError,
}: {
  state: WizardState;
  set: (patch: Partial<WizardState>) => void;
  errors: Record<string, string>;
  platform: Platform | undefined;
  platforms: Platform[];
  catalogError?: string | null;
}) {
  const [reveal, setReveal] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);

  // Saved-credential hint: if a stored git credential already covers the
  // entered repo (by full slug, else by host), the token becomes optional.
  // A repo-scoped match wins over a host-scoped one. Only probed for git
  // sources. Typing a token still overrides (usingSaved goes false).
  const { credentials } = useGitCredentials(state.sourceType === "git");
  const matchedCredential = useMemo(() => {
    if (state.sourceType !== "git" || !state.url.trim() || credentials.length === 0) {
      return null;
    }
    return matchCredential(credentials, {
      slug: slugFromRepoUrl(state.url),
      host: hostOf(state.url),
    });
  }, [credentials, state.url, state.sourceType]);
  const usingSaved = Boolean(matchedCredential) && !state.token.trim();

  return (
    <div className="space-y-4">
      {/* ── Source type toggle ──────────────────────────────────── */}
      <div>
        <h2 className="text-base font-medium">What are you indexing?</h2>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
          Point to a code repository or upload a document collection.
        </p>
        <div className="mt-3 grid gap-2 grid-cols-1 sm:grid-cols-2">
          <SourceTypeCard
            icon={<GitFork className="h-4 w-4" />}
            title="Git repository"
            desc="Index a GitHub, GitLab, Gitea, or any hosted repo."
            selected={state.sourceType === "git"}
            onSelect={() => set({ sourceType: "git" })}
          />
          <SourceTypeCard
            icon={<Database className="h-4 w-4" />}
            title="Document catalog"
            desc="Paste or upload text/Markdown files — no git URL needed."
            selected={state.sourceType === "catalog"}
            onSelect={() => set({ sourceType: "catalog" })}
          />
        </div>
      </div>

      {/* ── Git fields ─────────────────────────────────────────── */}
      {state.sourceType === "git" && (
        <>
          <Field
            label="Repository URL"
            hint="HTTPS or SSH"
            required
            error={errors.url}
          >
            <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]">
              <GitFork className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
              <Input
                type="text"
                placeholder="https://github.com/owner/repo"
                value={state.url}
                onChange={(e) => set({ url: e.target.value })}
                spellCheck={false}
                autoFocus
                className="flex-1 h-auto border-0 px-0 py-0 shadow-none bg-transparent font-mono placeholder:text-[hsl(var(--muted-foreground))] focus-visible:ring-0"
              />
            </div>
          </Field>

          <Field
            label="Platform"
            required
            hint="We auto-detect from the URL — change if needed"
          >
            <div className="grid gap-2 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3">
              {platforms.map((p) => (
                <PlatformTile
                  key={p.id}
                  platform={p}
                  selected={state.platform === p.id}
                  onSelect={() => set({ platform: p.id, platformLocked: true })}
                />
              ))}
            </div>
          </Field>

          {platform && (
            <Field
              label={platform.tokenLabel}
              hint="Optional · only needed for private repos"
            >
              <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]">
                <KeyRound className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
                <Input
                  type={reveal ? "text" : "password"}
                  placeholder={usingSaved ? "Saved credential will be used" : `Paste your ${platform.name} token`}
                  value={state.token}
                  onChange={(e) => set({ token: e.target.value })}
                  spellCheck={false}
                  autoComplete="off"
                  className="flex-1 h-auto border-0 px-0 py-0 shadow-none bg-transparent placeholder:text-[hsl(var(--muted-foreground))] focus-visible:ring-0"
                />
                <button
                  type="button"
                  onClick={() => setReveal((r) => !r)}
                  aria-label={reveal ? "Hide token" : "Show token"}
                  className="inline-flex items-center justify-center w-7 h-7 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]"
                >
                  {reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                </button>
              </div>
              {matchedCredential && (
                <div className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-[hsl(var(--primary))]/25 bg-[hsl(var(--primary))]/10 px-2 py-1 text-2xs text-[hsl(var(--primary-text))]">
                  <ShieldCheck className="h-3 w-3 shrink-0" />
                  <span>
                    Using saved credential for{" "}
                    <code className="font-mono">{matchedCredential.scope}</code>
                    {state.token.trim() && " — overridden by the token above"}
                  </span>
                </div>
              )}
              <div className="mt-2 flex items-start justify-between gap-3 flex-wrap">
                <div className="inline-flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
                  <Info className="h-3 w-3" />
                  Token is held in memory for this session only — never persisted.
                </div>
                <button
                  type="button"
                  onClick={() => setHelpOpen((s) => !s)}
                  className="inline-flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                >
                  <BookOpen className="h-3 w-3" />
                  How do I create a {platform.name} token?
                  {helpOpen ? (
                    <ChevronUp className="h-3 w-3" />
                  ) : (
                    <ChevronDown className="h-3 w-3" />
                  )}
                </button>
              </div>
              {helpOpen && (
                <div className="mt-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
                  <div className="flex items-start gap-3 flex-wrap">
                    <span
                      aria-hidden
                      className="inline-flex items-center justify-center w-8 h-8 rounded shrink-0"
                      style={{ background: platform.color }}
                    >
                      <PlatformIcon platformId={platform.id} className="h-4 w-4 text-white" />
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium">
                        Create a token on {platform.name}
                      </div>
                      <div className="text-2xs text-[hsl(var(--muted-foreground))] mt-0.5">
                        {platform.tokenScope}
                      </div>
                    </div>
                    {platform.tokenUrl && (
                      <a
                        href={platform.tokenUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-2xs text-[hsl(var(--primary-text))] hover:underline"
                      >
                        Open token settings
                        <ExternalLink className="size-3" />
                      </a>
                    )}
                  </div>
                  <ol className="mt-2.5 space-y-1.5 text-xs text-[hsl(var(--muted-foreground))] list-decimal pl-5">
                    {platform.tokenSteps.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ol>
                </div>
              )}
            </Field>
          )}
        </>
      )}

      {/* ── Catalog fields ─────────────────────────────────────── */}
      {state.sourceType === "catalog" && (
        <>
          <Field
            label="Workspace name"
            hint="Becomes the project slug"
            required
            error={errors.catalogName}
          >
            <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]">
              <Database className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
              <Input
                type="text"
                placeholder="my-knowledge-base"
                value={state.catalogName}
                onChange={(e) => set({ catalogName: e.target.value })}
                spellCheck={false}
                autoFocus
                className="flex-1 h-auto border-0 px-0 py-0 shadow-none bg-transparent placeholder:text-[hsl(var(--muted-foreground))] focus-visible:ring-0"
              />
            </div>
            {state.catalogName.trim() && (
              <p className="mt-1 text-2xs text-[hsl(var(--muted-foreground))]">
                Slug:{" "}
                <span className="font-mono text-[hsl(var(--foreground))]">
                  {slugifyName(state.catalogName) || "—"}
                </span>
              </p>
            )}
          </Field>

          <Field
            label="Documents"
            required
            error={errors.catalogDocs}
            hint="Paste content or upload .txt / .md files"
          >
            <CatalogDocsForm
              docs={state.catalogDocs}
              onChange={(docs) => set({ catalogDocs: docs })}
              error={catalogError ?? undefined}
            />
          </Field>
        </>
      )}
    </div>
  );
}

function SourceTypeCard({
  icon,
  title,
  desc,
  selected,
  onSelect,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={cn(
        "flex items-start gap-3 p-3.5 rounded-lg border text-left transition-all",
        selected
          ? "border-[hsl(var(--primary))] bg-[hsl(var(--primary))]/[0.06] ring-1 ring-[hsl(var(--primary))]/30"
          : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--border-strong))] hover:bg-[hsl(var(--accent))]/40"
      )}
    >
      <span className="text-[hsl(var(--primary))] mt-0.5 shrink-0">{icon}</span>
      <span className="flex-1 min-w-0">
        <span className="block text-sm font-medium">{title}</span>
        <span className="block text-2xs text-[hsl(var(--muted-foreground))]">{desc}</span>
      </span>
    </button>
  );
}
