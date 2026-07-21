"use client";

import { type ComponentPropsWithRef, forwardRef } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type TooltipIconButtonProps = ComponentPropsWithRef<typeof Button> & {
  tooltip: string;
  side?: "top" | "bottom" | "left" | "right";
};

export const TooltipIconButton = forwardRef<
  HTMLButtonElement,
  TooltipIconButtonProps
>(({ children, tooltip, side = "bottom", className, ...rest }, ref) => {
  return (
    <TooltipProvider>
      <Tooltip>
        {/* Radix Trigger uses asChild (not Base-UI `render`); children must
            live inside the cloned Button so the sr-only label rides along. */}
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            iconOnly
            {...rest}
            className={cn(
              "aui-button-icon size-6 p-1 active:scale-90",
              className,
            )}
            ref={ref}
          >
            {children}
            <span className="aui-sr-only sr-only">{tooltip}</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent side={side}>{tooltip}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
});

TooltipIconButton.displayName = "TooltipIconButton";
