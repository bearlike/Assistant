> ↑ [ui/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Search Chats — ui/search/

Scope: `ui/search/` — `SearchChatsScreen` + the pure `SessionSearchFilter`. v1 is a CLIENT-SIDE
title-only filter over the drawer's already-cached list (spec §6.8) — there is NO backend search call.

- **`SessionSearchFilter`** is a pure `internal object` (unit-testable without Compose). A blank query
  returns the FULL list unchanged (not empty); a non-blank query matches ONLY `SessionSummary.title`
  (`contains`, ignore-case), so a `null`-titled session is dropped from any search. It does NOT search
  message bodies.
- **`SearchChatsScreen`** reuses the SAME `SessionsViewModel` (`hiltViewModel()`) as the drawer, so it
  inherits that VM's loading/error/offline states — no separate fetch. The filter runs every keystroke
  inside `remember(sessions, query)`. It's a separate PUSHED route (no `ModalNavigationDrawer` chrome).
  The empty state is two-way: "No matches" (query non-blank) vs "No chats yet" (blank). Rows show
  `RelativeTime.formatShort` trailing meta, shared with [`ui/sessions/`](../sessions/CLAUDE.md).

**Session learnings for the search surface accrue here** — e.g. when/if v1's title-only client filter is
replaced by a real backend search, this is where that contract note belongs.
