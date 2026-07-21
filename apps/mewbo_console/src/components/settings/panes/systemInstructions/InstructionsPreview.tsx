/**
 * InstructionsPreview — the "render this template for surface X" card at the
 * bottom of `SystemInstructionsPane`. Lifted out of that file: it owns the
 * surface-tab strip and the rendered/error output, and knows nothing about
 * the editor or the save/reset lifecycle above it — the pane still owns
 * `previewSurface` state and the `usePreviewSystemInstructions()` mutation,
 * threaded down as plain props.
 */
import { Eye, Loader2 } from "lucide-react";

import type { SystemInstructionsPreviewResult } from "../../../../api/systemInstructions";
import { Button } from "../../../ui/button";
import { Tabs, TabsList, TabsTrigger } from "../../../ui/tabs";
import { ErrorAlert } from "../../../ErrorAlert";
import { subsectionTitleCls } from "../../styles";

/**
 * Surface ids whose display casing can't be derived (acronyms, camel-cased
 * brands). Everything else is title-cased from the raw id, so a surface this
 * console has never heard of still renders a readable tab rather than being
 * dropped. This is a LABEL table, not a list of surfaces: the surfaces
 * themselves come from the `surface` variable's `values` (see
 * `SystemInstructionsPane` — never hardcode a copy of that list here).
 */
const SURFACE_LABELS: Record<string, string> = {
  api: "API",
  cli: "CLI",
  mcp: "MCP",
  vcs: "VCS",
  github: "GitHub",
};

function surfaceLabel(id: string): string {
  return (
    SURFACE_LABELS[id] ??
    id
      .split(/[-_]/)
      .map((word) => SURFACE_LABELS[word] ?? word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ")
  );
}

export function InstructionsPreview({
  surfaces,
  previewSurface,
  onSurfaceChange,
  onPreview,
  pending,
  result,
}: {
  surfaces: string[];
  previewSurface: string;
  onSurfaceChange: (surface: string) => void;
  onPreview: () => void;
  pending: boolean;
  result: SystemInstructionsPreviewResult | undefined;
}) {
  return (
    <div className="rounded-lg border border-[hsl(var(--border))] p-4 space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <p className={subsectionTitleCls}>Preview</p>
          <p className="text-xs text-[hsl(var(--muted-foreground))]">
            See exactly what a session on this surface would get appended to its system prompt.
          </p>
        </div>
        <Button
          variant="neutral"
          size="sm"
          onClick={onPreview}
          disabled={pending || !previewSurface}
          leadingIcon={
            pending ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Eye className="w-3.5 h-3.5" />
            )
          }
        >
          {pending ? "Rendering…" : "Preview"}
        </Button>
      </div>

      {/* `h-auto flex-wrap` so a deployment with a dozen surfaces wraps to a
          second row instead of overflowing the card. */}
      {surfaces.length > 0 && (
        <Tabs value={previewSurface} onValueChange={onSurfaceChange}>
          <TabsList className="h-auto flex-wrap justify-start gap-1">
            {surfaces.map((surface) => (
              <TabsTrigger key={surface} value={surface}>
                {surfaceLabel(surface)}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      )}

      {result?.error ? (
        <ErrorAlert title="Template failed to render" error={result.error} fallback={result.error} />
      ) : result ? (
        <pre className="whitespace-pre-wrap rounded-md bg-[hsl(var(--code-body))] text-[hsl(var(--code-fg))] p-3 text-xs max-h-64 overflow-y-auto">
          {result.rendered || "(empty: the template rendered to nothing for this surface)"}
        </pre>
      ) : (
        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          Pick a surface and click Preview.
        </p>
      )}
    </div>
  );
}
