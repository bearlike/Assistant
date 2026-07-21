import { useState } from "react";
import { RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useRollbackApp } from "../../../hooks/useApps";
import type { AppVersion, AppVersionVerificationOutcome } from "../../../types/apps";
import { Badge } from "../../agents";
import { describeVersionSummary } from "../versionSummary";
import { RailEmpty, RailList, RailMeta, RailNote, RailRow } from "./rows";

/** Badge `color` per verification outcome — reuses the shared `Badge` (no new
 *  design tokens): pass rides `--success`, fail `--destructive`, skipped is
 *  a neutral muted tone (never a warning color — a skip is not a failure). */
const VERIFICATION_BADGE_COLOR: Record<AppVersionVerificationOutcome, string> = {
  pass: "emerald",
  fail: "red",
  skipped: "muted",
};

/** Mirrors the bare-glyph convention `RunRow` (`Runs.tsx`) already uses for run
 *  status, so outcome reads at a glance without relying on color alone. */
const VERIFICATION_GLYPH: Record<AppVersionVerificationOutcome, string> = {
  pass: "✓",
  fail: "✕",
  skipped: "–",
};

export function VersionsBody({
  appId,
  activeVersion,
  versions,
}: {
  appId: string;
  activeVersion: number;
  versions: AppVersion[];
}) {
  const rollback = useRollbackApp();
  const [target, setTarget] = useState<number | null>(null);
  // Newest first; an app always has at least its current version.
  const ordered = [...versions].sort((a, b) => b.version - a.version);

  return (
    <>
      {ordered.length === 0 ? (
        <RailEmpty>Version 1.</RailEmpty>
      ) : (
        <RailList>
          {ordered.map((v) => {
            const active = v.version === activeVersion;
            // Both additive/optional — an older row (recorded before this
            // field shipped) simply lacks one; render nothing, no placeholder.
            const summaryLine = v.summary ? describeVersionSummary(v.summary) : null;
            const verificationEntries = v.verification ? Object.entries(v.verification) : [];
            return (
              <RailRow
                key={v.version}
                active={active}
                // A version string is a LABEL, not machine text — it carries
                // its rank on weight and colour like every other row identity.
                label={<>v{v.version}</>}
                trailing={
                  <>
                    <RailMeta title={`Authored by ${v.author}`}>{v.author}</RailMeta>
                    {active && <RailMeta tone="active">active</RailMeta>}
                  </>
                }
                actions={
                  !active && (
                    <Button
                      variant="ghost"
                      size="sm"
                      iconOnly
                      aria-label={`Roll back to v${v.version}`}
                      title={`Roll back to v${v.version}`}
                      onClick={() => setTarget(v.version)}
                      className="text-[hsl(var(--muted-foreground))]"
                    >
                      <RotateCcw className="h-3 w-3" />
                    </Button>
                  )
                }
              >
                {v.note && <RailNote title={v.note}>{v.note}</RailNote>}
                {summaryLine && <RailNote title={summaryLine}>{summaryLine}</RailNote>}
                {verificationEntries.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1">
                    {verificationEntries.map(([name, outcome]) => (
                      <Badge key={name} color={VERIFICATION_BADGE_COLOR[outcome]}>
                        {VERIFICATION_GLYPH[outcome]} {name}
                      </Badge>
                    ))}
                  </div>
                )}
              </RailRow>
            );
          })}
        </RailList>
      )}

      <ConfirmDialog
        open={target !== null}
        title={`Roll back to v${target ?? ""}?`}
        description="The active version repoints to this snapshot. History is preserved, so the rollback itself becomes a new version and you can move forward again."
        confirmLabel="Roll back"
        pendingLabel="Rolling back…"
        confirmIcon={<RotateCcw className="h-3.5 w-3.5" />}
        pending={rollback.isPending}
        onCancel={() => setTarget(null)}
        onConfirm={() => {
          if (target == null) return;
          rollback.mutate({ appId, version: target }, { onSuccess: () => setTarget(null) });
        }}
      />
    </>
  );
}
