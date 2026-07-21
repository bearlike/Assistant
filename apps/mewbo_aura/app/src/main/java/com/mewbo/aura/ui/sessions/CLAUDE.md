> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Sessions View-State + Rail Helpers — ui/sessions/

Scope: `ui/sessions/` — **there is no list SCREEN here anymore** (the drawer,
[`ui/navigation/`](../navigation/CLAUDE.md), replaced it). This package holds `SessionsViewModel` +
`SessionsUiState` and the two PURE, unit-tested rail helpers (`RecentsFilter`, `SessionGrouping`) +
`RelativeTime`. Rail visual laws + provenance: DESIGN.md §3.

- **`RecentsFilter`** (`MOBILE_ONLY` / `ALL`) — the analogue of the console's `DEFAULT_VISIBLE_ORIGINS`.
  `MOBILE_ONLY` matches `origin == "mobile"`; **a `null` origin (pre-provenance session) is treated as
  NOT mobile → excluded by the default filter.** The filter default is `MOBILE_ONLY`, held in the VM
  (NOT in `SessionsUiState`) so it survives a refresh (which replaces the state) and resets on VM
  recreation. Filtering is applied CLIENT-SIDE by the drawer over the full fetched list (`GET /api/sessions`
  returns everything, so the filter can't starve).
- **`SessionsViewModel` — stale-while-revalidate.** The VM is recreated per chat back-stack
  entry, so it SEEDS its initial state from the `@Singleton` `SessionRepository`'s shared cache
  (`Loaded(cached)` when non-empty) and re-fetches in the background; `refresh()` likewise keeps a
  rendered list on screen and shows the skeleton (`Loading`) ONLY when the cache is genuinely EMPTY
  (first launch) — it never error-blanks. A failed background refresh WITH a non-empty cache degrades to
  `Loaded(offline = true)`, never `Error`; its `runCatching` manually re-throws `CancellationException`
  (the VM is created fresh per host and torn down mid-fetch, a fix). `renameSession`/`archiveSession`
  mutate the repository's cached list IN PLACE (instant UI); the trailing `refresh()` is server-truth
  reconciliation, not the source of the update. **No hard-delete exists — the API has none.**
- **`SessionGrouping`** — buckets `TODAY` / `PREVIOUS_7_DAYS` (`date >= today.minusDays(7) && date < today`) /
  `OLDER` in fixed display order (empty buckets dropped). Recency ts = `updatedAt.ifBlank { createdAt }`;
  an unparseable/blank ts sorts as `OLDER`, never crashes bucketing.
- **`RelativeTime`** — `format`/`formatShort` parse ONLY via `Timestamps.parseInstantOrNull`
  ([`data/model/CLAUDE.md`](../../data/model/CLAUDE.md) — the ONE ISO parser, since Android's bundled
  `java.time.Instant.parse` throws on the backend's numeric-offset form). `formatShort` uses `java.time`
  + hardcoded `Locale.US` for deterministic "Today"/"Yesterday" across CI hosts; `parseEpochMillis` is
  split out so parsing is plain-JUnit testable without `DateUtils`.
