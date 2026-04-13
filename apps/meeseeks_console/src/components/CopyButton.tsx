import { useState } from 'react';
import { Copy, Check } from 'lucide-react';
import { copyText } from '../utils/clipboard';
import { Button } from './ui/Button';

/**
 * Copy-to-clipboard button with icon-swap feedback. Uses the shared Button
 * primitive (ghost iconOnly sm) for consistent default styling. Callers may
 * append layout/theme overrides via `className` — TerminalCard, for example,
 * appends absolute-positioning and dark-theme color tweaks so the copy icon
 * sits correctly inside the terminal chrome.
 */
export function CopyButton({ text, className = '', children }: {
  text: string;
  className?: string;
  children?: React.ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await copyText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  const icon = copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />;
  return (
    <Button
      variant="ghost"
      size="sm"
      iconOnly={!children}
      leadingIcon={icon}
      onClick={handleCopy}
      aria-label="Copy"
      className={className}
    >
      {children}
    </Button>
  );
}
