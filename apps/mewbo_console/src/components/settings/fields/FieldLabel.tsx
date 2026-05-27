/**
 * FieldLabel — the single label atom for the Settings UI.
 *
 * Every template (FieldTemplate, widgets) renders its label through this so the
 * label typography lives in exactly one place (`labelCls` in `styles.ts`).
 */
import * as React from "react";

import { cn } from "@/lib/utils";
import { labelCls } from "../styles";

interface FieldLabelProps {
  htmlFor?: string;
  children: React.ReactNode;
  className?: string;
}

export function FieldLabel({ htmlFor, children, className }: FieldLabelProps) {
  return (
    <label htmlFor={htmlFor} className={cn(labelCls, className)}>
      {children}
    </label>
  );
}
