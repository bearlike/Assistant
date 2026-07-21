/**
 * "Flash, don't toast" timing, shared by the rail's two transient
 * confirmations: `Health.tsx`'s rearm result and `Pipelines.tsx`'s fire-now
 * result. Row and section chrome live in `rows.tsx` / `InstrumentRail.tsx`.
 */
export const FLASH_SUCCESS_MS = 3000;
export const FLASH_ERROR_MS = 4000;
