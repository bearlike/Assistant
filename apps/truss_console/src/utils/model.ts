/** Strip provider prefix: "anthropic/claude-sonnet-4-6" → "claude-sonnet-4-6" */
export function formatModelName(model: string): string {
  const slash = model.lastIndexOf('/');
  return slash >= 0 ? model.slice(slash + 1) : model;
}
