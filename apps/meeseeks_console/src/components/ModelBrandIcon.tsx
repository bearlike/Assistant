import { getProviderIcon } from '../utils/modelIcon';

type Props = {
  modelId: string;
  size?: number;
};

export function ModelBrandIcon({ modelId, size = 14 }: Props) {
  const icon = getProviderIcon(modelId);
  if (!icon) return null;
  const { Component, color } = icon;
  return <Component size={size} color={color} />;
}
