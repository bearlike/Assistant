import { useState } from 'react';
import { Copy, Check } from 'lucide-react';
import { copyText } from '../utils/clipboard';

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
  return (
    <button onClick={handleCopy} className={className} aria-label="Copy">
      {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
      {children}
    </button>
  );
}
