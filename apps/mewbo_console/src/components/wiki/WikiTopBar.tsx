/**
 * Secondary topbar for the wiki sub-product. Persistent across
 * Configure / Welcome / Indexing / Wiki / QA screens. (Landing has its own
 * card-shaped header instead.)
 *
 * Left: MewboWiki brand + repo slug + optional "Maintainer Edited" pill.
 * Right: optional Edit Wiki popover + Copy-link button.
 *
 * Theme toggle lives in the main NavBar — not duplicated here.
 */

import { useEffect, useState } from "react";
import { useLocation } from "wouter";
import {
  ArrowLeft,
  BadgeCheck,
  Check,
  Copy,
  FileText,
  Info,
  Network,
  Pencil,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";

import { BrandMark } from "./BrandMark";
import { RepoLink } from "./RepoLink";
import { buildHref, type PlatformId } from "./router";

interface WikiTopBarProps {
  /** Canonical slug (``host/owner/repo`` or legacy ``owner/repo``). */
  repo?: string;
  /** Persisted repo URL — preferred over slug-derived ``https://host/...``. */
  repoUrl?: string;
  /** Carried into the Graph button's URL — same params as the rest of /wiki/*. */
  platform?: PlatformId;
  maintainerEdited?: boolean;
  showEditWiki?: boolean;
  showBackToAll?: boolean;
}

export function WikiTopBar({
  repo,
  repoUrl,
  platform,
  maintainerEdited,
  showEditWiki,
  showBackToAll,
}: WikiTopBarProps) {
  const [, navigate] = useLocation();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(t);
  }, [copied]);

  const onCopyLink = async () => {
    const url = window.location.href;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      // execCommand fallback for non-secure contexts
      const ta = document.createElement("textarea");
      ta.value = url;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        // give up silently
      }
      document.body.removeChild(ta);
    }
    setCopied(true);
  };

  return (
    <div className="border-b border-[hsl(var(--border))] bg-[hsl(var(--card))]/60 backdrop-blur-sm">
      <div className="max-w-[1400px] mx-auto px-4 sm:px-6 h-12 flex items-center gap-3">
        {showBackToAll && (
          <button
            type="button"
            onClick={() => navigate("/wiki")}
            className="inline-flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors -ml-1 px-1 py-1"
            aria-label="Back to all wikis"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">All wikis</span>
          </button>
        )}

        <button
          type="button"
          onClick={() => navigate("/wiki")}
          className="inline-flex items-center gap-1.5 hover:opacity-80 transition-opacity"
        >
          <span className="text-[hsl(var(--primary))]">
            <BrandMark size={18} />
          </span>
          <span className="text-sm font-semibold tracking-tight">MewboWiki</span>
        </button>

        {repo && (
          <span className="font-mono text-xs text-[hsl(var(--muted-foreground))] truncate min-w-0">
            <RepoLink slug={repo} repoUrl={repoUrl} />
          </span>
        )}

        {maintainerEdited && (
          <span className="inline-flex items-center gap-1 px-2 h-6 rounded-full border border-[hsl(var(--primary))]/30 bg-[hsl(var(--primary))]/10 text-[10px] text-[hsl(var(--primary))]">
            <BadgeCheck className="h-3 w-3" />
            <span className="hidden md:inline">Maintainer Edited</span>
          </span>
        )}

        <div className="flex-1" />

        <div className="flex items-center gap-1.5">
          {showEditWiki && (
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  leadingIcon={<Pencil className="h-3.5 w-3.5" />}
                  aria-label="Edit wiki"
                >
                  <span className="hidden sm:inline">Edit Wiki</span>
                </Button>
              </PopoverTrigger>
              <PopoverContent
                align="end"
                sideOffset={6}
                className="w-80 p-0 rounded-lg border-[hsl(var(--border-strong))] bg-[hsl(var(--card))]"
              >
                <div className="flex items-center gap-2 px-3.5 py-2.5 border-b border-[hsl(var(--border))]">
                  <Info className="h-3.5 w-3.5 text-[hsl(var(--primary))]" />
                  <span className="text-sm font-medium">Steer wiki generation</span>
                </div>
                <div className="px-3.5 py-3 space-y-2.5">
                  <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed">
                    To customise this wiki, add or edit one of these files in
                    the repository root and re-index:
                  </p>
                  <ul className="space-y-1.5">
                    {[".mewbo/wiki.json", ".devin/wiki.json"].map((p) => (
                      <li
                        key={p}
                        className="inline-flex items-center gap-2 px-2 py-1 rounded-md bg-[hsl(var(--muted))]/50 w-full"
                      >
                        <FileText className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />
                        <code className="font-mono text-xs text-[hsl(var(--foreground))]">{p}</code>
                      </li>
                    ))}
                  </ul>
                  <p className="text-[11px] text-[hsl(var(--muted-foreground))] pt-1">
                    Either file is recognised — pick whichever fits your tooling.
                  </p>
                </div>
              </PopoverContent>
            </Popover>
          )}

          {repo && (
            <Button
              variant="ghost"
              size="sm"
              asChild
              aria-label="Open knowledge graph in new tab"
              title="Knowledge graph"
            >
              <a
                href={buildHref({ kind: "graph", slug: repo, platform })}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5"
              >
                <Network className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Graph</span>
              </a>
            </Button>
          )}

          <Button
            variant={copied ? "primary" : "primary"}
            size="sm"
            onClick={onCopyLink}
            aria-live="polite"
            leadingIcon={
              copied ? (
                <Check className="h-3.5 w-3.5" />
              ) : (
                <Copy className="h-3.5 w-3.5" />
              )
            }
            className={cn(
              copied &&
                "!bg-emerald-500 hover:!bg-emerald-500/90 !text-white"
            )}
          >
            {copied ? "Copied" : "Copy link"}
          </Button>
        </div>
      </div>
    </div>
  );
}

// Helper for the small "X" close affordance reused in popover-style cards.
export function CloseButton({ onClick, label = "Dismiss" }: { onClick: () => void; label?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className="inline-flex items-center justify-center w-5 h-5 rounded-md text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] transition-colors"
    >
      <X className="h-3 w-3" />
    </button>
  );
}
