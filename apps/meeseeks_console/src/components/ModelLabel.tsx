import { formatModelName } from '../utils/model';
import { ModelBrandIcon } from './ModelBrandIcon';

type Props = {
  modelId?: string | null;
  iconSize?: number;
  className?: string;
};

/**
 * Inline brand icon + short model name. Returns null when `modelId` is empty
 * so callers can drop it in without a null-check. Pass `className` for pill /
 * text styling; this component owns the flex layout + icon. When no brand
 * icon matches the model, `ModelBrandIcon` renders null and the text sits
 * flush left (no phantom gap, since `gap-1` only applies between children).
 */
export function ModelLabel({ modelId, iconSize = 10, className = '' }: Props) {
  if (!modelId) return null;
  return (
    <span className={`inline-flex items-center gap-1 ${className}`}>
      <ModelBrandIcon modelId={modelId} size={iconSize} />
      {formatModelName(modelId)}
    </span>
  );
}
