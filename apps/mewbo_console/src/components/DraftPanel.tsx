/**
 * DraftPanel — streaming LLM draft surface.
 *
 * Lets a user enter a free-form query (+ optional workspace slug) and watch
 * the LLM draft stream token-by-token from POST /v1/draft/stream.
 *
 * Design notes:
 *   - Right-side instrument-panel vocabulary: `rounded-md` cards, no
 *     `rounded-full` on non-state-container elements.
 *   - Colors exclusively from CSS variable tokens — no literal hsl() or
 *     Tailwind palette names.
 *   - Tokens render on arrival: `useDraftStream` appends deltas in the
 *     reducer; there is no synthetic typewriter timer.
 *   - `streaming` gates the streaming-indicator and disables the submit
 *     button. The cancel button aborts the stream by setting `input` to
 *     `null`, which triggers `useDraftStream`'s cleanup effect.
 *
 * Atomic-class pattern: all state lives in this single component, exposed as
 * instance-local state attrs + one class-static sub-renderer (the output
 * region). No external state manager.
 */

import { useState, useCallback, useRef, useId } from "react";
import { Loader2, X, FileText, ChevronRight } from "lucide-react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Textarea } from "./ui/textarea";
import { useDraftStream } from "../hooks/useDraftStream";
import type { DraftStreamInput } from "../api/draft";
import { cn } from "../lib/utils";

// ── Output region ─────────────────────────────────────────────────────────────

interface DraftOutputProps {
  text: string;
  streaming: boolean;
  done: boolean;
  error: string | null;
}

/** Renders the live draft output area — pure display, no state. */
function DraftOutput({ text, streaming, done, error }: DraftOutputProps) {
  const isEmpty = !text && !error && !streaming;

  if (isEmpty) {
    return (
      <div className="flex flex-col items-center justify-center h-full min-h-[160px] text-center gap-2 text-[hsl(var(--muted-foreground))]">
        <FileText className="w-8 h-8 opacity-30" />
        <p className="text-sm">Your draft will appear here as tokens stream in.</p>
      </div>
    );
  }

  return (
    <div className="relative h-full min-h-[160px]">
      {error && (
        <div className="mb-3 px-3 py-2 rounded-md border border-[hsl(var(--destructive))]/40 bg-[hsl(var(--destructive))]/10 text-sm text-[hsl(var(--destructive))]">
          {error}
        </div>
      )}
      <div
        className={cn(
          "whitespace-pre-wrap text-sm leading-relaxed text-[hsl(var(--foreground))]",
          streaming && "after:content-['▌'] after:ml-0.5 after:animate-pulse after:text-[hsl(var(--primary))]",
        )}
        aria-live="polite"
        aria-label="Draft output"
      >
        {text}
      </div>
      {done && !error && text && (
        <p className="mt-3 text-xs text-[hsl(var(--muted-foreground))] flex items-center gap-1">
          <ChevronRight className="w-3 h-3" />
          Draft complete
        </p>
      )}
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

/**
 * Full-page draft streaming panel. Mounts under the /draft route.
 */
export function DraftPanel() {
  // Controlled form fields
  const [query, setQuery] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [model, setModel] = useState("");

  // The committed input drives the stream. Setting it to null resets / cancels.
  const [streamInput, setStreamInput] = useState<DraftStreamInput | null>(null);

  const queryId = useId();
  const workspaceId = useId();
  const modelId = useId();

  // Ref so cancel handler always sees latest value without a stale closure.
  const streamInputRef = useRef(streamInput);
  streamInputRef.current = streamInput;

  const { text, streaming, done, error } = useDraftStream(streamInput);

  const handleSubmit = useCallback(() => {
    const q = query.trim();
    if (!q) return;
    setStreamInput({
      query: q,
      workspace: workspace.trim() || undefined,
      model: model.trim() || undefined,
    });
  }, [query, workspace, model]);

  const handleCancel = useCallback(() => {
    setStreamInput(null);
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !streaming) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit, streaming],
  );

  const hasOutput = Boolean(text || error || streaming);

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">
        {/* Header */}
        <div>
          <h1 className="text-lg font-semibold text-[hsl(var(--foreground))]">
            Draft stream
          </h1>
          <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">
            Stream an LLM draft token-by-token. Optionally scope to a workspace.
          </p>
        </div>

        {/* Input card */}
        <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 space-y-4">
          {/* Query */}
          <div className="space-y-1.5">
            <Label htmlFor={queryId}>Query</Label>
            <Textarea
              id={queryId}
              placeholder="Describe what you'd like drafted…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={streaming}
              rows={4}
              className="resize-none"
            />
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Press <kbd className="font-mono">Ctrl+Enter</kbd> to submit.
            </p>
          </div>

          {/* Optional fields row */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor={workspaceId}>Workspace slug (optional)</Label>
              <Input
                id={workspaceId}
                placeholder="my-repo-slug"
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                disabled={streaming}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={modelId}>Model override (optional)</Label>
              <Input
                id={modelId}
                placeholder="e.g. openai/gpt-4o"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                disabled={streaming}
              />
            </div>
          </div>

          {/* Action row */}
          <div className="flex items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              onClick={handleSubmit}
              disabled={streaming || !query.trim()}
              leadingIcon={
                streaming ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : undefined
              }
            >
              {streaming ? "Streaming…" : "Stream draft"}
            </Button>
            {streaming && (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleCancel}
                leadingIcon={<X className="w-3.5 h-3.5" />}
                aria-label="Cancel stream"
              >
                Cancel
              </Button>
            )}
          </div>
        </div>

        {/* Output card — only render once there's something to show */}
        {hasOutput && (
          <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4">
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wide">
                Output
              </span>
              {streaming && (
                <span className="inline-flex items-center gap-1.5 text-xs text-[hsl(var(--primary))]">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Streaming
                </span>
              )}
            </div>
            <DraftOutput
              text={text}
              streaming={streaming}
              done={done}
              error={error}
            />
          </div>
        )}
      </div>
    </div>
  );
}
