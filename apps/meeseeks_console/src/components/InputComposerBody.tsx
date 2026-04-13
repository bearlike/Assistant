import React from 'react';
import {
  Mic,
  ArrowUp,
  ChevronsRight,
  Paperclip,
  Square,
  Loader2,
  Maximize2,
} from 'lucide-react';
import { QueryMode } from '../types';
import { Button } from './ui/button';

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
 * Shared composer body used by home, detail, and full-screen Dialog InputBar
 * variants. Owns the visual structure (attached-file chip → textarea →
 * toolbar) but holds NO state — the parent InputBar drives every value via
 * props. Only chrome (border, position, max-width) lives in the wrapper.
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

  return (
    <>
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
          onKeyDown={onKeyDown}
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
          {plusMenu}
          {queryMode === 'plan' && (
            <Button variant="neutral" size="sm" className="uppercase tracking-wide text-[10px]">
              Plan
            </Button>
          )}
          {configMenu}
        </div>

        <div className={`flex items-center flex-shrink-0 ${p.toolbarRightGap}`}>
          {showVoice && (
            <Button variant="ghost" size="md" iconOnly aria-label="Voice input">
              <Mic className="w-4 h-4" />
            </Button>
          )}
          {showStop && isRunning && (
            <Button
              variant="ghost"
              size="md"
              iconOnly
              tone="danger"
              onClick={onStop}
              aria-label="Stop run"
            >
              <Square className="w-4 h-4" />
            </Button>
          )}
          <Button
            variant="primary"
            size="md"
            iconOnly
            onClick={onSubmit}
            aria-label={isRunning ? 'Steer agent' : 'Send query'}
            title={isRunning ? 'Steer the running agent' : undefined}
            disabled={isSubmitting || !inputValue.trim()}
          >
            {isSubmitting ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : isRunning ? (
              <ChevronsRight className="w-4 h-4" />
            ) : (
              <ArrowUp className="w-4 h-4" />
            )}
          </Button>
        </div>
      </div>
    </>
  );
}
