/**
 * Floating Q&A dock — fixed at the bottom of the viewport, viewport-
 * centered, max 720px wide. Textarea + compact ModelPicker + clay primary
 * send button. Enter submits; Shift+Enter inserts a newline.
 */

import { useRef, useState } from "react";

import { MicButton } from "@/components/MicButton";
import { composerInputCls, ComposerSendButton, ComposerShell } from "@/components/ui/composer-shell";
import { cn } from "@/lib/utils";

import type { QaMode } from "./api/types";
import { ModelPicker } from "./ModelPicker";
import { QaModeControl } from "./QaModeControl";

interface QADockProps {
  placeholder: string;
  model: string;
  onModelChange: (m: string) => void;
  mode: QaMode;
  onModeChange: (mode: QaMode) => void;
  onAsk: (question: string) => void;
}

export function QADock({ placeholder, model, onModelChange, mode, onModeChange, onAsk }: QADockProps) {
  const [value, setValue] = useState("");
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const submit = () => {
    const text = value.trim();
    if (!text) return;
    onAsk(text);
    setValue("");
    // restore minimal height
    if (taRef.current) taRef.current.style.height = "auto";
  };

  return (
    <ComposerShell
      // Centered on the CONTENT PANE, not the full viewport: the left nav
      // rail (`--rail-w` on the AppLayout root, 0/48/272px mobile/collapsed/
      // expanded — see TurnScroller.tsx for the same offset applied to a
      // left-anchored satellite) shifts the pane's optical center right by
      // half the rail's width. `left: (100vw + rail-w) / 2` is that shifted
      // center; the width cap subtracts rail-w too so the dock never bleeds
      // under the rail on a narrow viewport with the rail expanded.
      className="fixed bottom-4 left-[calc((100vw+var(--rail-w,0px))/2)] -translate-x-1/2 z-40 w-[min(720px,calc(100vw-var(--rail-w,0px)-2rem))]"
      surface={{ elevation: "elev-2", halo: "strong" }}
      bodyClassName={cn("p-2.5", "bg-[hsl(var(--card))]/95 backdrop-blur-md [box-shadow:var(--elev-3)]")}
      top={
        <textarea
          ref={taRef}
          rows={1}
          placeholder={placeholder}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            const el = e.currentTarget;
            el.style.height = "auto";
            el.style.height = Math.min(160, el.scrollHeight) + "px";
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          className={cn(
            composerInputCls(),
            "w-full resize-none bg-transparent leading-6 px-2 py-1 mb-1.5 outline-none placeholder:text-[hsl(var(--muted-foreground))] text-[hsl(var(--foreground))]"
          )}
        />
      }
      toolbarLeft={
        <div className="inline-flex items-center gap-1">
          <ModelPicker variant="compact" value={model} onChange={onModelChange} />
          <QaModeControl value={mode} onChange={onModeChange} />
        </div>
      }
      toolbarRight={
        <>
          <MicButton value={value} onChange={setValue} />
          <ComposerSendButton
            onClick={submit}
            active={Boolean(value.trim())}
            shape="square"
            aria-label="Ask question"
          />
        </>
      }
    />
  );
}
