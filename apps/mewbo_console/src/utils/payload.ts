/**
 * Typed field reads for untrusted event/tool payloads — the shared notation for
 * the `logs.ts` and `timeline.ts` parse seams. Each returns the value only when
 * the payload is an object that carries `key` with the matching runtime type,
 * else `undefined`; a non-object payload (null included) reads as `undefined`
 * rather than throwing.
 *
 * These are a pure notation for `typeof obj[key] === "T" ? obj[key] : undefined`
 * — supply a default at the call site with `??` (which defaults on null/
 * undefined only, so `""` / `0` / `false` still pass through unchanged).
 */
function readField(payload: unknown, key: string): unknown {
  if (payload != null && typeof payload === "object") {
    return (payload as Record<string, unknown>)[key];
  }
  return undefined;
}

export function readString(payload: unknown, key: string): string | undefined {
  const value = readField(payload, key);
  return typeof value === "string" ? value : undefined;
}

export function readNumber(payload: unknown, key: string): number | undefined {
  const value = readField(payload, key);
  return typeof value === "number" ? value : undefined;
}

export function readBool(payload: unknown, key: string): boolean | undefined {
  const value = readField(payload, key);
  return typeof value === "boolean" ? value : undefined;
}
