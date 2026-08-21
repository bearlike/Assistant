> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Sessions View-State + Rail Helpers — ui/sessions/

Scope: `ui/sessions/` — `SessionsViewModel`/`SessionsUiState` plus the pure, unit-tested rail helpers
`RecentsFilter`, `SessionGrouping`, `RelativeTime`. There is no list screen here; the drawer
([`ui/navigation/`](../navigation/CLAUDE.md)) is the recents surface.

- **`RecentsFilter`** (`MOBILE_ONLY` / `ALL`). `MOBILE_ONLY` matches `origin == "mobile"`; **a `null`
  origin is NOT mobile and is excluded by the default filter.** The default is `MOBILE_ONLY`, held in
  the VM and NOT in `SessionsUiState`, so it survives a refresh (which replaces the state) and resets
  on VM recreation. Filtering is CLIENT-SIDE over a **BOUNDED** fetch: `SessionRepository`'s
  `RECENTS_FETCH_LIMIT` caps how many candidates `GET /api/sessions` examines, so this filter narrows
  a window, not the store. **It therefore CAN starve** — a window carrying no mobile-origin session
  renders "No mobile chats yet" while older ones exist beyond it. Accepted because the server orders
  newest-first and a session Aura creates is mobile-origin, so the window keeps this device's own
  history by construction; the bound was measured at the depth where mobile yield saturates (the
  constant's KDoc carries the numbers). `RecentsFilter.ALL` re-reads the SAME window, never a wider
  fetch. **Do not "fix" a starved rail by raising the bound** — `GET /api/sessions` has no `origin`
  parameter, so real mobile scoping is a backend change; fetching more to filter harder client-side
  re-opens the unbounded 3 MB transfer the bound exists to close.
- **`SessionsViewModel` is stale-while-revalidate.** The VM is recreated per chat back-stack entry, so
  it SEEDS its initial state from the `@Singleton` `SessionRepository`'s shared cache
  (`Loaded(cached)` when non-empty) and re-fetches in the background. `refresh()` likewise keeps the
  rendered list on screen; the skeleton (`Loading`) shows ONLY when the cache is genuinely empty. A
  failed background refresh with a non-empty cache degrades to `Loaded(offline = true)`, never
  `Error`. Its `runCatching` must manually re-throw `CancellationException` — the VM is created fresh
  per host and torn down mid-fetch. `renameSession`/`archiveSession` mutate the repository's cached
  list IN PLACE for instant UI; the trailing `refresh()` is server-truth reconciliation, not the source
  of the update. **No hard-delete exists — the API has none.**
- **`SessionGrouping`** buckets `PINNED` / `TODAY` / `PREVIOUS_7_DAYS`
  (`date >= today.minusDays(7) && date < today`) / `OLDER` in fixed display order, dropping empty
  buckets. Recency ts = `updatedAt.ifBlank { createdAt }`; an unparseable or blank ts sorts as `OLDER`
  rather than crashing bucketing.
- **A pinned session gets its OWN bucket ahead of every date bucket and is EXCLUDED from date bucketing
  entirely** — never both, or the row renders twice and reads as a duplicate rather than emphasis. Date
  bucketing alone cannot keep a pin visible: a session pinned three weeks ago sorts into `OLDER`,
  exactly where a pin exists to prevent it from hiding. Ordered `pinnedAt` descending; an unparseable
  stamp sorts to the end of the bucket. **Pinning is applied AFTER `RecentsFilter.matches` has narrowed
  the list** — it is an ORDERING, never a way around the filter, which is what keeps a pinned
  non-mobile session from leaking past `MOBILE_ONLY`.
- **Pin/unpin** — `SessionsViewModel.setPinned` mirrors `archiveSession`'s shape: patch the cached row
  in place (instant re-section), then a trailing `refresh()` reconciles. The wire route is
  `POST`/`DELETE` on ONE path (`api/sessions/{id}/pin`) — there is no `/unpin` path.
- **`RelativeTime`** parses ONLY via `Timestamps.parseInstantOrNull`
  ([`data/model/CLAUDE.md`](../../data/model/CLAUDE.md)), because Android's bundled
  `java.time.Instant.parse` throws on the backend's numeric-offset form. `formatShort` hardcodes
  `Locale.US` so "Today"/"Yesterday" are deterministic across CI hosts; `parseEpochMillis` is split out
  so parsing is plain-JUnit testable without `DateUtils`.
