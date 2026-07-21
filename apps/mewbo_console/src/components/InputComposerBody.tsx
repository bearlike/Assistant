import React, { useState } from 'react';
import {
  Mic,
  ArrowUp,
  ArrowUpRight,
  ClipboardList,
  Paperclip,
  Plus,
  Square,
  Loader2,
  Maximize2,
} from 'lucide-react';
import { QueryMode } from '../types';
import { cn } from '../lib/utils';
import { Button } from './ui/button';
import { composerInputCls } from './ui/composer-shell';
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
  expanded: boolean;
  isRunning?: boolean;
  /** Live snapshot fed into the running-state strip — phase/agents/tokens/elapsed. */
  runStatus?: RunStatus;
  onStop?: () => void;
  queryMode: QueryMode;
  /** Toggle plan/act. Rendered as a visible footer pill (plan pill stays while a run is in flight if active). */
  onTogglePlanMode: () => void;
  /** Open the native file picker for attachments. */
  onAttach: () => void;
  onToggleFullScreen?: () => void;
  attachedFiles: File[];
  onClearAttachments: () => void;
  /** Pre-rendered session-config menu (ConfigMenu — owns its own popover). */
  configMenu: React.ReactNode;
  /** Pre-rendered model selector (ModelSelector — owns its own popover). */
  modelSelector: React.ReactNode;
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
        chip: 'px-1.5 pt-1 flex items-center gap-2',
        chipMaxName: 'max-w-[180px]',
        textareaWrap: 'relative px-2.5 py-1.5',
        toolbar: 'flex items-center justify-between px-1',
        toolbarLeftGap: 'gap-1',
        toolbarRightGap: 'gap-1.5',
        maximizeAnchor: 'absolute top-1.5 right-1.5 opacity-60 hover:opacity-100',
      };
    case 'detail':
      return {
        chip: 'px-1.5 pt-1',
        chipMaxName: 'max-w-[180px]',
        textareaWrap: 'relative px-2.5 py-1',
        toolbar: 'flex items-center justify-between px-1',
        toolbarLeftGap: 'gap-1',
        toolbarRightGap: 'gap-1.5',
        maximizeAnchor: 'absolute top-1 right-1 opacity-60 hover:opacity-100',
      };
    case 'dialog':
    default:
      return {
        chip: 'px-4 pt-3 pb-0',
        chipMaxName: 'max-w-[240px]',
        textareaWrap: 'flex-1 px-4 py-3 overflow-hidden',
        toolbar:
          'flex items-center justify-between px-3 py-2 border-t border-[hsl(var(--border))]',
        toolbarLeftGap: 'gap-2',
        toolbarRightGap: 'gap-2',
        maximizeAnchor: '',
      };
  }
}

/**
 * Slot at the top of the composer that hosts RunTelemetry while a run is in
 * progress. The slot owns the chrome (inset hairline + padding); the telemetry
 * content (phase + agents + tokens + elapsed) comes from the shared
 * `<RunTelemetry>` component, which is also used by the workspace FlowerSpinner
 * — same data, two windows on one truth (E3, P8).
 */
function RunIndicator({ status, active }: { status?: RunStatus; active: boolean }) {
  if (!active) return null;
  return (
    <div className="border-b border-[hsl(var(--border))] px-2 pb-1.5">
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
          className="px-3"
        >
          <Square className="mr-1 h-3.5 w-3.5" />
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
 * Send / Queue button. Shows text in the detail composer ("Send" idle,
 * "Queue" while steering a live run); collapses to an icon-only circular
 * button in the home + dialog composers (vendor look). Running tints it with
 * the steering accent so the flip from Send→Queue reads at a glance.
 */
function SendButton({
  variant,
  isRunning,
  isSubmitting,
  disabled,
  onSubmit,
}: {
  variant: InputComposerBodyProps['variant'];
  isRunning: boolean;
  isSubmitting: boolean;
  disabled: boolean;
  onSubmit: () => void;
}) {
  const hasText = variant === 'detail';
  const icon = isSubmitting ? (
    <Loader2 className="h-3.5 w-3.5 animate-spin" />
  ) : isRunning ? (
    <ArrowUpRight className="h-3.5 w-3.5" />
  ) : (
    <ArrowUp className="h-3.5 w-3.5" />
  );
  return (
    <Button
      variant="primary"
      size="sm"
      iconOnly={!hasText}
      onClick={onSubmit}
      aria-label={isRunning ? 'Queue steering message' : 'Send query'}
      title={isRunning ? 'Queue steering message (⌘↵)' : 'Send (⌘↵)'}
      disabled={disabled}
      leadingIcon={hasText ? icon : undefined}
      className={cn(
        hasText && 'px-3.5',
        isRunning &&
          'bg-[hsl(var(--permission))] text-[hsl(var(--primary-foreground))] hover:bg-[hsl(var(--permission))]/90',
      )}
    >
      {hasText ? (isRunning ? 'Queue' : 'Send') : icon}
    </Button>
  );
}

/**
 * Shared composer body used by home, detail, and full-screen Dialog InputBar
 * variants. Owns the visual structure (run-indicator → attached-file chip →
 * textarea → toolbar) but holds NO state — the parent InputBar drives every
 * value via props. Only chrome (the rounded card, position, max-width) lives in
 * the outer wrapper. Footer toolbar (§I): left = [+ attach] [session config]
 * [model] [plan]; right = [mic] [stop] [send].
 */
export function InputComposerBody(props: InputComposerBodyProps) {
  const {
    variant,
    inputValue,
    onInputChange,
    onSubmit,
    onKeyDown,
    isSubmitting,
    expanded,
    isRunning = false,
    runStatus,
    onStop,
    queryMode,
    onTogglePlanMode,
    onAttach,
    onToggleFullScreen,
    attachedFiles,
    onClearAttachments,
    configMenu,
    modelSelector,
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

  // Plan pill is visible as a toggle when idle; while a run is in flight it is
  // hidden UNLESS plan is active, where it stays as a locked indicator.
  const showPlanPill = !isRunning || queryMode === 'plan';

  return (
    <>
      <RunIndicator status={runStatus} active={(isRunning || isSubmitting) && variant === 'detail'} />

      {attachedFiles.length > 0 && (
        <div className={p.chip}>
          <div className="bg-[hsl(var(--accent))] rounded px-2 py-1 text-xs text-[hsl(var(--foreground))] inline-flex items-center gap-2 w-fit">
            <Paperclip className="w-3 h-3" />
            <span className={`truncate ${p.chipMaxName}`}>{attachedFiles[0].name}</span>
            {attachedFiles.length > 1 && (
              <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                +{attachedFiles.length - 1}
              </span>
            )}
            <button onClick={onClearAttachments} className="hover:opacity-70" aria-label="Clear attachments">
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
              ? `w-full h-full bg-transparent border-none outline-none caret-[hsl(var(--primary))] text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] ${composerInputCls()} resize-none`
              : `w-full bg-transparent border-none outline-none caret-[hsl(var(--primary))] text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] ${composerInputCls()} resize-none max-h-[200px] pr-7 ${TEXTAREA_TRANSITION} ${
                  expanded ? 'min-h-[72px]' : 'min-h-[24px]'
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
          {/* Mid-run, the composer narrows to a steering instrument — attach
              and the plan toggle drop away; the session-config + model triggers
              stay (locked) so the run's context is still legible. (§I, P5) */}
          {!isRunning && (
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              onClick={onAttach}
              aria-label="Attach files"
              title="Attach files"
            >
              <Plus className="h-4 w-4" />
            </Button>
          )}
          {configMenu}
          {modelSelector}
          {showPlanPill && (
            <Button
              variant={queryMode === 'plan' ? 'neutral' : 'ghost'}
              size="sm"
              onClick={onTogglePlanMode}
              disabled={isRunning}
              aria-pressed={queryMode === 'plan'}
              aria-label="Toggle plan mode"
              title={
                isRunning
                  ? 'Plan mode is locked while the agent is running'
                  : 'Plan mode — review a plan before acting'
              }
              className={cn('gap-1.5 px-2.5', queryMode === 'plan' && 'text-[hsl(var(--primary-text))]')}
            >
              <ClipboardList className="h-3.5 w-3.5" />
              <span>Plan</span>
            </Button>
          )}
        </div>

        <div className={`flex items-center flex-shrink-0 ${p.toolbarRightGap}`}>
          {showVoice && !isRunning && (
            <Button variant="ghost" size="sm" iconOnly aria-label="Voice input" title="Voice input">
              <Mic className="h-4 w-4" />
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
          <SendButton
            variant={variant}
            isRunning={isRunning}
            isSubmitting={isSubmitting}
            disabled={isSubmitting || !inputValue.trim()}
            onSubmit={onSubmit}
          />
        </div>
      </div>
    </>
  );
}
