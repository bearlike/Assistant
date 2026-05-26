/**
 * Floating Q&A dock — fixed at the bottom of the viewport, viewport-
 * centered, max 720px wide. Textarea + compact ModelPicker + clay primary
 * send button. Enter submits; Shift+Enter inserts a newline.
 */

import { useRef, useState } from "react";
import { ArrowUp } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { ModelPicker } from "./ModelPicker";

interface QADockProps {
  placeholder: string;
  model: string;
  onModelChange: (m: string) => void;
  onAsk: (question: string) => void;
}

export function QADock({ placeholder, model, onModelChange, onAsk }: QADockProps) {
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
    <div
      className={cn(
        "fixed bottom-4 left-1/2 -translate-x-1/2 z-40 w-[min(720px,calc(100vw-2rem))]",
        "rounded-xl border border-[hsl(var(--border-strong))] bg-[hsl(var(--card))]/95 backdrop-blur-md shadow-[0_8px_32px_rgba(0,0,0,0.25)]"
      )}
    >
      <div className="p-2.5">
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
          className="w-full resize-none bg-transparent text-sm leading-6 px-2 py-1 outline-none placeholder:text-[hsl(var(--muted-foreground))] text-[hsl(var(--foreground))]"
        />
        <div className="flex items-center justify-between mt-1.5">
          <ModelPicker variant="compact" value={model} onChange={onModelChange} />
          <Button
            type="button"
            variant="primary"
            size="sm"
            iconOnly
            onClick={submit}
            disabled={!value.trim()}
            aria-label="Ask question"
          >
            <ArrowUp className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
    </div>
  );
}
