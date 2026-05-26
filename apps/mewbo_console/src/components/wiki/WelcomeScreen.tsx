/**
 * Welcome / "Repository not indexed" screen.
 *
 * Reached only from a project card that doesn't have a wiki yet. The
 * primary action redirects to the configure wizard with the repo URL
 * pre-filled — that's the path that actually starts a new indexing run.
 * (Earlier versions of this screen collected an email and pushed the
 * request onto a notify queue; that queue + dispatcher were removed.)
 */

import { useLocation } from "wouter";
import { GitFork, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";

import { BrandMark } from "./BrandMark";
import { RepoLink } from "./RepoLink";
import { WikiTopBar } from "./WikiTopBar";
import { PlatformIcon } from "./configure-wizard/PlatformIcon";
import { buildHref, type PlatformId } from "./router";
import { parseSlug } from "./slug";

interface WelcomeScreenProps {
  /** Canonical slug — fully qualified ``host/owner/repo``. */
  slug: string;
  /** Platform (software flavor) of the source repo, if known. */
  platform?: PlatformId;
}

export function WelcomeScreen({ slug, platform }: WelcomeScreenProps) {
  const [, navigate] = useLocation();
  const parsed = parseSlug(slug);
  const repo = parsed?.repo ?? slug;
  const host = parsed?.host;

  // Best-effort canonical URL — github-style works for github/gitlab/gitea;
  // self-hosted repos still parse correctly via the host prefix. The wizard
  // accepts any of these and will re-derive `platform` server-side.
  const repoUrl = host && parsed
    ? `https://${host}/${parsed.owner}/${parsed.repo}`
    : `https://${slug}`;

  const onStart = () => {
    navigate(buildHref({ kind: "configure", url: repoUrl }));
  };

  return (
    <div className="flex flex-col flex-1 overflow-y-auto">
      <WikiTopBar repo={slug} showBackToAll />
      <div className="flex-1 px-4 sm:px-6 py-10 sm:py-14">
        <div className="max-w-[680px] mx-auto">
          {/* Repo card */}
          <div className="rounded-xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-5 flex items-start gap-4">
            <div className="shrink-0 inline-flex items-center justify-center w-12 h-12 rounded-xl bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary))]">
              <BrandMark size={28} />
            </div>
            <div className="flex-1 min-w-0">
              <h2 className="text-base font-semibold truncate">{repo}</h2>
              <span className="inline-block mt-0.5 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                <RepoLink slug={slug} />
              </span>
              <p className="mt-2 text-sm text-[hsl(var(--muted-foreground))] leading-relaxed">
                We don't have a wiki for this repository yet. Configure
                indexing and we'll generate one for you.
              </p>
              <div className="mt-3 flex items-center gap-1.5 flex-wrap">
                {platform && (
                  <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60 text-[11px] text-[hsl(var(--muted-foreground))]">
                    <PlatformIcon platformId={platform} className="h-2.5 w-2.5" />
                    {platform}
                  </span>
                )}
                {host && (
                  <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60 text-[11px] font-mono text-[hsl(var(--muted-foreground))]">
                    {host}
                  </span>
                )}
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted))]/60 text-[11px] text-[hsl(var(--muted-foreground))]">
                  <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-amber-400" />
                  Auto-detect language
                </span>
              </div>
            </div>
          </div>

          {/* Hero */}
          <h1 className="mt-10 text-[clamp(28px,4vw,42px)] font-semibold tracking-[-0.02em] [text-wrap:balance]">
            Repository not indexed
          </h1>
          <p className="mt-2 text-sm text-[hsl(var(--muted-foreground))] [text-wrap:pretty] leading-relaxed">
            Indexing lets you explore code structure, find documentation,
            and ask questions about this repository.
          </p>
          <p className="mt-1.5 text-xs text-[hsl(var(--muted-foreground))]/80">
            Indexing typically takes 2–10 minutes once it starts.
          </p>

          <div className="mt-5">
            <Button
              type="button"
              variant="primary"
              size="md"
              onClick={onStart}
              leadingIcon={<Sparkles className="h-3.5 w-3.5" />}
            >
              Index Repository
            </Button>
          </div>

          <div className="mt-5 inline-flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
            <GitFork className="h-3.5 w-3.5" />
            <span>Once indexed, you'll have full access to code exploration and search.</span>
          </div>
        </div>
      </div>
    </div>
  );
}
