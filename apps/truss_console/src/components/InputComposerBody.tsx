import React, { useState } from 'react';
import {
  Mic,
  ArrowUp,
  ArrowUpRight,
  Paperclip,
  Square,
  Loader2,
  Maximize2,
} from 'lucide-react';
import { QueryMode } from '../types';
import { Button } from './ui/button';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import type { RunStatus } from './InputBar';
import { RunTelemetry } from './RunTelemetry';

/** Textarea min-height transition — actual min-h toggled via JS state, not CSS focus. */
const TEXTAREA_TRANSITION = 'transition-[min-height] duration-200 ease-out';

export interface InputComposerBodyProps {
  /** Variant controls textarea sizing, paddings, and whether full-height fills the container. */
  variant: 'home' | 'detail' | 'dialog';
  inputValue: string;
  onInputChange: (value: string) => void;
  onSubmit: () => void;
  onKeyDown?: (event: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  isSubmitting: boolean;
  isExpanded: boolean;
  isRunning?: boolean;
  /** Live snapshot fed into the running-state strip — phase/agents/tokens/elapsed. */
  runStatus?: RunStatus;
  onStop?: () => void;
  onTogglePlusMenu?: (open: boolean) => void;
  queryMode: QueryMode;
  onToggleFullScreen?: () => void;
  attachedFiles: File[];
  onClearAttachments: () => void;
  /** Pre-rendered Plus menu (DropdownMenu trigger + items). */
  plusMenu: React.ReactNode;
  /** Pre-rendered Config menu. */
  configMenu: React.ReactNode;
  textareaRef: React.RefObject<HTMLTextAreaElement>;
  placeholder: string;
  ariaLabel: string;
  showVoice?: boolean;
  showStop?: boolean;
  showMaximize?: boolean;
  autoFocus?: boolean;
  onFocus?: () => void;
  onBlur?: () => void;
  /** When true, textarea fills available height (used by Dialog body). */
  fillHeight?: boolean;
}

function paddings(variant: InputComposerBodyProps['variant']) {
  switch (variant) {
    case 'home':
      return {
        chip: 'px-4 pt-3 pb-0 flex items-center gap-2',
        chipMaxName: 'max-w-[180px]',
        textareaWrap: 'relative px-4 py-3',
        textareaSize: 'text-base',
        toolbar: 'flex items-center justify-between px-2 pb-1',
        toolbarLeftGap: 'gap-2',
        toolbarRightGap: 'gap-2',
        maximizeAnchor: 'absolute top-2 right-2 opacity-60 hover:opacity-100',
      };
    case 'detail':
      return {
        chip: 'px-3 pt-2 pb-0',
        chipMaxName: 'max-w-[180px]',
        textareaWrap: 'relative px-3 py-2',
        textareaSize: 'text-sm',
        toolbar: 'flex items-center justify-between px-1 pb-1',
        toolbarLeftGap: 'gap-1',
        toolbarRightGap: 'gap-1',
        maximizeAnchor: 'absolute top-1 right-1 opacity-60 hover:opacity-100',
      };
    case 'dialog':
    default:
      return {
        chip: 'px-4 pt-3 pb-0',
        chipMaxName: 'max-w-[240px]',
        textareaWrap: 'flex-1 px-4 py-3 overflow-hidden',
        textareaSize: 'text-base',
        toolbar:
          'flex items-center justify-between px-3 py-2 border-t border-[hsl(var(--border))]',
        toolbarLeftGap: 'gap-2',
        toolbarRightGap: 'gap-2',
        maximizeAnchor: '',
      };
  }
}

/**
 * Bordered slot at the top of the composer that hosts RunTelemetry while a
 * run is in progress. The slot owns the chrome (hairline + padding); the
 * telemetry content (phase + agents + tokens + elapsed) comes from the
 * shared `<RunTelemetry>` component, which is also used by the workspace
 * FlowerSpinner — same data, two windows on one truth (E3, P8).
 */
function RunIndicator({ status, active }: { status?: RunStatus; active: boolean }) {
  if (!active) return null;
  return (
    <div className="border-b border-[hsl(var(--border))] pl-[14px] pr-3 py-2">
      <RunTelemetry data={status} variant="compact" />
    </div>
  );
}

/**
 * Stop button + confirmation popover. Lives inline with the Send button in
 * the running composer toolbar (matches the design's "single home for
 * input + run state" thesis). Click the Stop pill to open the confirm; Esc
 * inside the textarea also opens it (wired up at the parent via onKeyDown).
 */
function StopWithConfirm({
  open,
  onOpenChange,
  agents,
  onStop,
}: {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  agents: number;
  onStop?: () => void;
}) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          tone="danger"
          aria-label="Stop run"
          title="Stop run (Esc)"
          className="h-8 rounded-md px-3 text-xs"
        >
          <Square className="mr-1 h-3 w-3" />
          Stop
        </Button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="end"
        className="w-60 p-3 text-xs"
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <div className="mb-2 text-sm font-medium text-[hsl(var(--foreground))]">
          Stop the run?
        </div>
        <div className="space-y-1.5">
          <Button
            variant="primary"
            tone="danger"
            size="sm"
            className="w-full justify-center"
            onClick={() => {
              onOpenChange(false);
              onStop?.();
            }}
          >
            Stop all{agents > 1 ? ` (${agents} agents)` : ''}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-center"
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Shared composer body used by home, detail, and full-screen Dialog InputBar
 * variants. Owns the visual structure (run-indicator → attached-file chip →
 * textarea → toolbar) but holds NO state — the parent InputBar drives every
 * value via props. Only chrome (border, position, max-width) lives in the
 * outer wrapper.
 */
export function InputComposerBody(props: InputComposerBodyProps) {
  const {
    variant,
    inputValue,
    onInputChange,
    onSubmit,
    onKeyDown,
    isSubmitting,
    isExpanded,
    isRunning = false,
    runStatus,
    onStop,
    queryMode,
    onToggleFullScreen,
    attachedFiles,
    onClearAttachments,
    plusMenu,
    configMenu,
    textareaRef,
    placeholder,
    ariaLabel,
    showVoice = true,
    showStop = false,
    showMaximize = true,
    autoFocus = false,
    onFocus,
    onBlur,
    fillHeight = false,
  } = props;
  const p = paddings(variant);
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false);

  // Esc inside the textarea opens the same Stop confirmation popover —
  // matches the design's "single home for run state" thesis. We compose
  // around the parent-supplied onKeyDown so existing handlers (Enter to
  // submit, Cmd-Enter to send) still fire.
  const composedKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (isRunning && e.key === 'Escape' && showStop) {
      e.preventDefault();
      setStopConfirmOpen(true);
      return;
    }
    onKeyDown?.(e);
  };

  return (
    <>
      <RunIndicator status={runStatus} active={isRunning && variant === 'detail'} />

      {attachedFiles.length > 0 && (
        <div className={p.chip}>
          <div className="bg-[hsl(var(--accent))] rounded px-2 py-1 text-xs text-[hsl(var(--foreground))] inline-flex items-center gap-2 w-fit">
            <Paperclip className="w-3 h-3" />
            <span className={`truncate ${p.chipMaxName}`}>{attachedFiles[0].name}</span>
            {attachedFiles.length > 1 && (
              <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                +{attachedFiles.length - 1}
              </span>
            )}
            <button onClick={onClearAttachments} className="hover:opacity-70">
              ×
            </button>
          </div>
        </div>
      )}

      <div className={p.textareaWrap}>
        <textarea
          ref={textareaRef}
          value={inputValue}
          onChange={(e) => onInputChange(e.target.value)}
          onKeyDown={composedKeyDown}
          onFocus={onFocus}
          onBlur={onBlur}
          placeholder={placeholder}
          aria-label={ariaLabel}
          disabled={isSubmitting}
          autoFocus={autoFocus}
          rows={fillHeight ? undefined : 1}
          className={
            fillHeight
              ? `w-full h-full bg-transparent border-none outline-none text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] ${p.textareaSize} resize-none`
              : `w-full bg-transparent border-none outline-none text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] ${p.textareaSize} resize-none max-h-[200px] pr-7 ${TEXTAREA_TRANSITION} ${
                  isExpanded ? 'min-h-[72px]' : 'min-h-[24px]'
                }`
          }
        />
        {showMaximize && onToggleFullScreen && (
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={onToggleFullScreen}
            aria-label="Expand editor"
            className={p.maximizeAnchor}
          >
            <Maximize2 className="w-3.5 h-3.5" />
          </Button>
        )}
      </div>

      <div className={p.toolbar}>
        <div className={`flex items-center min-w-0 flex-nowrap ${p.toolbarLeftGap}`}>
          {/* Mid-run, the composer narrows to "proj-pill | spacer | Stop | Send" —
              the user's hands are here, so the toolbar should read as a steering
              instrument, not a setup panel. (§I, P5) */}
          {!isRunning && plusMenu}
          {!isRunning && queryMode === 'plan' && (
            <Button variant="neutral" size="sm" className="uppercase tracking-wide text-[10px]">
              Plan
            </Button>
          )}
          {configMenu}
        </div>

        <div className={`flex items-center flex-shrink-0 ${p.toolbarRightGap}`}>
          {showVoice && !isRunning && (
            <Button variant="ghost" size="md" iconOnly aria-label="Voice input">
              <Mic className="w-4 h-4" />
            </Button>
          )}
          {showStop && isRunning && (
            <StopWithConfirm
              open={stopConfirmOpen}
              onOpenChange={setStopConfirmOpen}
              agents={runStatus?.agents ?? 0}
              onStop={onStop}
            />
          )}
          <Button
            variant="primary"
            size="md"
            onClick={onSubmit}
            aria-label={isRunning ? 'Queue steering message' : 'Send query'}
            title={isRunning ? 'Queue steering message (⌘↵)' : 'Send (⌘↵)'}
            disabled={isSubmitting || !inputValue.trim()}
            className={`h-8 rounded-md px-3 text-xs ${
              isRunning
                ? 'bg-[hsl(var(--permission))] text-white hover:bg-[hsl(var(--permission))]/90'
                : ''
            }`}
            leadingIcon={
              isSubmitting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : isRunning ? (
                <ArrowUpRight className="h-3.5 w-3.5" />
              ) : (
                <ArrowUp className="h-3.5 w-3.5" />
              )
            }
          >
            {variant === 'detail' ? (isRunning ? 'Queue' : 'Send') : null}
          </Button>
        </div>
      </div>
    </>
  );
}
