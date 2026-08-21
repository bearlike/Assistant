> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md) · children: [api](api/CLAUDE.md) · [sse](sse/CLAUDE.md) · [model](model/CLAUDE.md) · [repo](repo/CLAUDE.md) · [device](device/CLAUDE.md) · [settings](settings/CLAUDE.md) · [update](update/CLAUDE.md)

# Aura Data Layer — hub

Scope: `data/` — API client, SSE, domain model, repos, device tools, settings. The full
field-for-field contract is verified against `mewbo_api/backend.py` + `mewbo_core` source.
**No Compose/UI imports in this package, ever.** This file is the thin hub: the CROSS-PACKAGE
invariants live here, the per-package detail lives in the child that owns it — read the deepest one
that applies.

| Sub-package | Owns |
|---|---|
| [`api/`](api/CLAUDE.md) | `AuraApi` Retrofit interface + DTOs; the `Response<>`-wrapped-vs-plain-DTO throw distinction; 200-vs-202 send-route branching |
| [`sse/`](sse/CLAUDE.md) | `SessionStreamClient`: backoff, full-backlog replay, bare `data:` frames, the `trySend`+`terminated`-drop hazard |
| [`model/`](model/CLAUDE.md) | `SessionEvent`, `TranscriptReducer` (the layout-enforcement layer), THE POISON ANCHOR, `PromotedTools`, `ComposerScope`, `Timestamps` |
| [`repo/`](repo/CLAUDE.md) | Repositories: `RunRepository` (`@Singleton`, `live()`, `errorFor`, `RunNotifications`), `SessionRepository` (fork/retry), `SessionContext`, `ConnectionProbe` |
| [`device/`](device/CLAUDE.md) | `device_*` catalog/handlers/executor/dispatch; the two-layer gate; the activity-launch importance gate; screen control at shell UID ([`device/shizuku/`](device/shizuku/CLAUDE.md)) |
| [`settings/`](settings/CLAUDE.md) | `SettingsStore` keys + defaults; `KeystoreCipher` API-key-at-rest |
| [`update/`](update/CLAUDE.md) | the in-app updater: one GitHub-shaped release client for both forges, the asset-name scheme, download-and-verify, the `PackageInstaller` seam, the signature-chain trap |

## Layering (cross-package)

`SessionStreamClient` (sse) owns ALL OkHttp-SSE mechanics; repos compose it + `AuraApi` and expose only
`Flow<SessionEvent>`/domain objects to the UI. Retrofit for REST; kotlinx.serialization with
`ignoreUnknownKeys = true` (and `explicitNulls = false`, [`di/`](../di/CLAUDE.md)) everywhere. The
network stack is built ONCE with a runtime URL-rewriting interceptor — see `di/CLAUDE.md`.

## The one cross-package invariant that spans repo + device: advertise AND answer at the SAME seam

A capability advertised to the backend is serviced at the seam EVERY host shares, never at one host's
ViewModel. `RunRepository.sendQuery` advertises the `device_*` tools on every `/query`
(`DeviceToolCatalog.availableTools`, filtered by runtime permission AND the per-tool `DeviceToolGate`
toggle), and `RunRepository.live()` builds `DeviceToolDispatch` INTO the flow it hands out — so every
path that FOLLOWS a run (chat, the assist overlay, the notification watcher) ANSWERS a
`device_tool_call` by construction. Advertising in `sendQuery` while wiring the answer into a ViewModel
cost a 66.8s overlay turn (two 30s server timeouts) once. Mechanics + the pipeline-not-subscriber and
subscriber-is-not-executor rules: [`repo/CLAUDE.md`](repo/CLAUDE.md) +
[`device/CLAUDE.md`](device/CLAUDE.md); the AOSP importance chain:
[`voice/CLAUDE.md`](../voice/CLAUDE.md) § "Device tools from the overlay".

**The same `live()` seam carries the turn-completion notification watch**
([`notify/`](../notify/CLAUDE.md)) as a third, passive follower — which is why `RunRepository` is
`@Singleton`. The notifier never advertises or answers device tools.

## What a SESSION does to that binding — the four flows, and where each loses it

Advertising is per-REQUEST, and only `sendQuery` carries `device_tools`. Every other way a user
reaches a session either re-advertises or inherits, and the failures are silent because absence and
never-declared are the same value on the wire. Recorded per flow, because "it stopped working when I
came back" was diagnosed four separate times before this was written down:

| Flow | Carries `device_tools`? |
|---|---|
| New session, first message | YES — `sendQuery` |
| Follow-up while IDLE | YES — `SendDecision` routes to `sendQuery` |
| Follow-up while a run is LIVE | **NO** — routes to `send` → `/message`, which has no `context` field at all |
| Retry / fork | **NO** — `/recover` and `/fork` carry no context either |
| Cold start onto a running session | **NO** — `bind` only re-opens the stream |

The three NOs are not bugs on their own: the server re-derives from the newest persisted `context`
event. They become bugs when that event says nothing about device tools, which is why the server
reads it with `payload_key` narrowing rather than taking the newest payload whole
([`apps/mewbo_api/CLAUDE.md`](../../../../../../../mewbo_api/CLAUDE.md)). **Two core writers emit a
context event carrying only their own key** — a plan approval and a recovery re-inject — so before
the narrowing, approving a plan silently revoked device tools for every later turn.

**The retry case displaces its failure by one turn**, which is what made it read as random: the
`/recover` run derives grants BEFORE the gating-only event is appended, so the retry works and the
NEXT `/message` binds nothing.

**The capability header and the tool list cannot disagree within one request** — both derive from
`availableTools()` — **but they diverge across time in both directions**, because the header rides
every request and is unioned into a sticky spec while the tools ride `/query` only and are re-read.
Shizuku dies mid-session ⇒ the header drops `device_control` while the persisted tools still bind;
Shizuku is up but a gating-only context event is newest ⇒ the header carries the capability and zero
tools bind, so the model activates a playbook for tools it does not have.

**The durable cure is the grant** ([`device/CLAUDE.md`](device/CLAUDE.md) § "The grant"), which makes
control session state instead of a per-request derivation. This section describes what the request
path still does underneath it.

**Ask-user questions are the DELIBERATE exception to auto-answer-in-`live()` — do not "fix" them into
it.** The `ask_user` capability is advertised unconditionally on the `X-Mewbo-Capabilities` header
(`di/DataModule`'s AuthInterceptor, beside `stlite`) because Aura can always render the question card
and POST an answer. But unlike a `device_tool_call`, the SERVICER is a human tap, not code: the card
renders from reducer state ([`model/`](model/CLAUDE.md)), and the tap flows
ViewModel → `RunRepository.answerQuestion` → `POST /questions/{callId}/answer` (200 → `Resolved`,
404/409 → `AlreadyResolved` — settle silently, answered elsewhere). Wiring a dispatcher for it into
`live()`'s pipeline would AUTO-answer on the user's behalf — the exact opposite of the feature. The
same-seam law governs capabilities serviced BY CODE; a human-in-the-loop capability's seam is the
event → card → POST loop, which every follower shares via the reducer.
