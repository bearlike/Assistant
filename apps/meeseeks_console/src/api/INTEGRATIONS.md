# API Integrations

Frontend → backend mapping (Meeseeks API):

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
