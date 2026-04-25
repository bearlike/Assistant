export function getErrorMessage(error: unknown, fallback = "Something went wrong") {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  if (typeof error === "string" && error.trim()) {
    return error;
  }
  return fallback;
}
export function logApiError(context: string, error: unknown) {
  const message = getErrorMessage(error, "Unexpected API error");
  // eslint-disable-next-line no-console
  console.error(`[api] ${context} failed: ${message}`);
  return message;
}