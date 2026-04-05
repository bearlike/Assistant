import type { ElementType } from 'react';
import Claude from '@lobehub/icons/es/Claude';
import DeepSeek from '@lobehub/icons/es/DeepSeek';
import Gemini from '@lobehub/icons/es/Gemini';
import Meta from '@lobehub/icons/es/Meta';
import Minimax from '@lobehub/icons/es/Minimax';
import OpenAI from '@lobehub/icons/es/OpenAI';
import Qwen from '@lobehub/icons/es/Qwen';
import Zhipu from '@lobehub/icons/es/Zhipu';

/**
 * Brand-icon mapping for model IDs. First match wins (order matters).
 *
 * Powered by @lobehub/icons (React components with official brand assets).
 * - Colored variants (e.g. `Claude.Color`) render with brand gradients and
 *   need no `color` prop.
 * - Monochrome components (e.g. `OpenAI`) use `currentColor` so they inherit
 *   the parent's text color — plays well with our themed foreground.
 *
 * To extend: append a new entry. Either
 *   `{ match: 'mistral', Component: Mistral.Color }`
 * or with an explicit color override
 *   `{ match: 'llama', Component: Meta, color: '#1877F2' }`
 *
 * `Component` is typed as `ElementType` to avoid a @types/react version
 * mismatch between @lobehub/icons and the console's React types.
 */
export type ProviderIcon = {
  /** Lowercase substring matched against the model ID. */
  match: string;
  /** Lobe icon component (use `.Color` variant where available). */
  Component: ElementType;
  /** Color for monochrome components; omit for colored variants. */
  color?: string;
};

export const PROVIDER_ICONS: ProviderIcon[] = [
  { match: 'claude', Component: Claude.Color },
  { match: 'gemini', Component: Gemini.Color },
  { match: 'deepseek', Component: DeepSeek.Color },
  { match: 'llama', Component: Meta.Color },
  { match: 'minimax', Component: Minimax.Color },
  { match: 'qwen', Component: Qwen.Color },
  { match: 'zhipu', Component: Zhipu.Color },
  { match: 'glm', Component: Zhipu.Color },
  { match: 'z.ai', Component: Zhipu.Color },
  { match: 'zai', Component: Zhipu.Color },
  { match: 'gpt', Component: OpenAI, color: 'currentColor' },
  { match: 'openai', Component: OpenAI, color: 'currentColor' },
];

export function getProviderIcon(modelId: string): ProviderIcon | null {
  const lower = modelId.toLowerCase();
  return PROVIDER_ICONS.find((i) => lower.includes(i.match)) ?? null;
}
