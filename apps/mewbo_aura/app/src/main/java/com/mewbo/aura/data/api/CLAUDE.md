> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura REST Contract — data/api/

Scope: `data/api/` — `AuraApi` (the Retrofit interface) and its DTOs. The full field-for-field wire
contract lives in the DTOs below. Headers are NOT set here — they live in [`di/DataModule`](../../di/CLAUDE.md)'s
`AuthInterceptor`/`BaseUrlInterceptor`. The SSE stream endpoint is deliberately NOT in this interface
— it's raw OkHttp owned by [`data/sse/`](../sse/CLAUDE.md).

## `Response<>`-wrapped vs plain-DTO return is load-bearing

- **Plain-DTO returns** (`createSession`, `listSessions`, `renameSession`, `archiveSession`, `getEvents`,
  `getModels`, `uploadAttachments`, `getProjects`, `getTools`) rely on Retrofit's **implicit throw** —
  any non-2xx surfaces as `HttpException` automatically. That IS the error path.
- **`Response<>`-wrapped returns** exist ONLY where the caller must branch on the 2xx status code
  itself: `sendMessage`, `query`, `recoverSession`, `forkSession`, `postDeviceToolResult`. Removing the
  wrapper would re-arm the implicit throw and break the code-branch.

Two send routes need code-branching because BOTH outcomes are 2xx: `sendMessage` — **200 = a new run
started, 202 = steered an already-active run** (`enqueued` is `true` in BOTH and is NOT a discriminator);
`query` — **202 = async run accepted, 200 = a slash command handled inline** (`/status`, `/terminate`).
`recoverSession`/`forkSession` read `409` (run active) / `410` (`session_terminated` envelope) off the
`Response`; the repo's `errorFor`/degrade-to-null handle them ([`data/repo/CLAUDE.md`](../repo/CLAUDE.md)).

## DTO traps (the deployed API lags the contract — decode tolerantly)

- **Every DTO field defaults tolerantly.** `SessionSummaryDto` requires only `session_id`; the deployed
  API never sends `updated_at`, so `toDomain()` falls back `updatedAt ?: createdAt ?: ""`. `running:
  Boolean` is the real liveness signal.
- `terminated`/`terminated_at` ride on BOTH `SessionSummaryDto` and `SessionEventsResponseDto`.
- **`SessionEventsResponseDto.events` is decoded as `List<JsonElement>`, NOT `List<SessionEvent>`** — so
  a single malformed frame can't fail the whole HTTP decode; each is mapped through the resilient
  `SessionEvent.decode` at the repo seam.
- `postDeviceToolResult` returns raw `Response<ResponseBody>` deliberately — a 200 body may legitimately
  be empty and running it through the converter would risk a decode failure on the success path. 200
  resolved / 403 bad token / 404 unknown call / 409 already consumed — all terminal, never retried.
- Multipart attachments: `@Part files: List<MultipartBody.Part>` (repeated field) + optional
  `@Part("model")` for eager (400) vision rejection; response records go verbatim into `/query`'s
  `attachments`.
- `ApiErrorEnvelope`/`ApiErrorBody` fields all default, so a non-envelope error body decodes to empty
  rather than throwing DURING error handling.
