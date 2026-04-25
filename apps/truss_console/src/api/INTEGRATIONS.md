# API Integrations

Frontend → backend mapping (Truss API):

- `GET /api/sessions` → `listSessions()` → `useSessions()`
- `POST /api/sessions` → `createSession()` → `useSessions()`
- `POST /api/sessions/{id}/query` → `postQuery()` → `useSessionQuery()`
- `GET /api/sessions/{id}/events` → `fetchEvents()` → `useSessionEvents()`
- `POST /api/sessions/{id}/archive` → `archiveSession()` → `useSessions()`
- `DELETE /api/sessions/{id}/archive` → `unarchiveSession()` → `useSessions()`
- `GET /api/tools` → `listTools()` → `useMcpTools()`

Notes:
- API auth uses `X-API-Key` (set via `VITE_API_KEY`).
- Slash commands are sent via `postQuery()` (e.g. `/terminate`, `/status`).
- `POST /api/webhooks/<platform>` is used by chat platform adapters (e.g. Nextcloud Talk) and uses HMAC auth, not the API key. Channel sessions appear in the console session list like any other session.
