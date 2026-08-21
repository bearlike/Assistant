> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo IDE broker — Project Guidance

Scope: `apps/mewbo_ide/`. The Node/TypeScript service that owns the docker socket
and creates per-session `code-server` containers for the Python API.

A *thin product surface*, same tier as `mewbo_api`: it composes, hosts no reusable
engine, **imports nothing from another app and is imported by nothing** — the API
talks to it over HTTP, the only coupling that exists. The `README.md` beside this
file is the operator-facing reference (routes, config table, wire shapes); this
file is the why.

## The one rule the whole service exists to enforce

**The broker CONSTRUCTS the container spec. It never accepts one.**

Image, binds, network, `Memory`, `NanoCpus`, `PidsLimit`, entrypoint, labels and the
container NAME all come from `BrokerConfig`. Exactly three caller-supplied values
reach a container: the `password` (into a field the broker chooses), the resolved
`workspace_path` (into a bind SOURCE the broker chooses) and the deadline epoch
(into a file the broker writes).

If you find yourself threading a fourth caller value into `IdeContainers.buildSpec`,
stop — that is the hole this service was built to close, and it closes it by
construction rather than by validation.

- **`password` is opaque** — the broker never interprets it, only places it. It
  stays caller-minted because the API already persists it and hands it to the
  browser; minting it here would force a placeholder-then-update write ordering for
  no security gain, since a compromised API can read it from its own store anyway.
- **`.strict()` on every body is load-bearing.** It turns a smuggled `image`,
  `binds` or `network` into a clean 400 rather than a silently ignored field, which
  reads identically to a quietly honoured one.

**A generic docker socket proxy cannot replace this.** A socket proxy filters by
verb and path, but the container spec lives in the request BODY — `Binds`,
`Privileged`, `Image`, `NetworkMode`, `CapAdd` — so allowing container creation at
all allows all of them. Restricting the body means encoding what a legitimate Mewbo
IDE container looks like, in a config language with no types and no tests. The
broker instead exposes four verbs that accept coordinates, so there is no spec to
filter.

## The sweep filters by LABEL, never by name prefix

`IdeContainers.sweep` lists with `filters: {label: ["mewbo.kind=web-ide"]}`. The
nginx reverse proxy is `container_name: mewbo-ide-proxy` and matches the
`mewbo-ide-` prefix, so a name-based sweep reaps it and takes the whole Web IDE
feature down on every boot.

The label is **re-checked per item** inside the loop rather than trusted to the
daemon-side filter — a redundant check is cheap and this mistake is unrecoverable.
`FakeDockerClient.honorFilters = false` exists so that second guard is provable
rather than assumed.

⚠️ **The sweep may depend only on `mewbo.kind` and `mewbo.session_id`.** Labels are
debug-only and are never read back to hydrate state; the deadline comes from the
FILE. Containers created by other writers carry a different label set (e.g.
`mewbo.max_deadline` rather than the broker's `mewbo.expires_at`, and a
`mewbo.project_name` the broker does not know). Depend on any label beyond those two
and a deploy fails to reap exactly the containers it stranded.

## The allowlist is required, and the broker must MOUNT what it allows

`MEWBO_IDE_ALLOWED_ROOTS` has no default and an empty value stops the process: a
fail-open on a bind-mount source is whole-host read/write, and refusing to boot is
diagnosable where silently accepting every path is not.

Two consequences that read like bugs and are not:

- **The broker must bind-mount the allowed roots at identical paths.**
  `WorkspaceAllowlist.resolve` calls `realpath` in the broker's OWN mount namespace,
  so a root it cannot see resolves to nothing and every workspace under it 403s. The
  403 is correct; the cause is deployment. Resolving in a real namespace is also
  what makes the symlink check real — a string prefix test needs no filesystem and
  catches nothing.
- **A relative root refuses to boot rather than resolving against the process cwd** —
  anchoring a containment boundary to whatever directory the container started in is
  the same fail-open in a different costume.

A root that does not exist yet is normalized rather than fatal: a mount that has not
appeared should not stop the boot, and a path no `realpath` can produce never
matches anything.

## ⚠️ `MEWBO_IDE_BROKER_HOST` defaults to `0.0.0.0`, and "hardening" it is an outage

`MEWBO_IDE_BROKER_HOST` is the address the broker binds **inside its own
container**, where loopback is a private network namespace: docker forwards a
published port to the container's bridge IP, never to its loopback, and a sibling
container resolves that same bridge IP. Bind `127.0.0.1` and the broker listens
where nothing can reach it **while the container healthcheck, which runs inside the
container, still passes** — a green healthcheck over an unreachable service.

Reachability containment comes from the loopback-scoped compose publish and the
shared secret on every `/v1` route, not from this variable.

## The watchdog string is a cross-language contract

`WATCHDOG_CMD` must stay byte-identical to the constant of the same name in
`apps/mewbo_api/src/mewbo_api/ide.py`. It is the shell that runs as PID 1 inside
every IDE container, and a single changed space changes what runs.

`containers.test.ts` **reads the Python file off disk and compares** rather than
asserting against a retyped literal, which would only prove the literal was copied
correctly. If that test fails, one of the two sides moved — find out which before
touching either.

`{sid}` is interpolated with no quoting, and none is wanted: the only safe input is
one with no shell metacharacters at all, which is why the session id is
regex-validated to `^[a-f0-9]{32}$` at the route before it reaches this code.

## Ordering rules that look arbitrary and are not

- **The deadline file is written BEFORE the container is created.** Docker
  materializes a missing bind source as a **directory**, so creating first leaves
  `/mewbo/deadline` a directory, `cat` fails forever, and the watchdog kills the
  container on its first tick.
- **The file holds bare epoch seconds with NO trailing newline.** The watchdog does
  `[ $(date +%s) -lt $(cat /mewbo/deadline) ]`; `sh`'s integer comparison refuses a
  token it cannot parse as a number, so a stray newline turns the guard into a
  permanent error and the loop exits immediately — killing the container at startup.
  `deadlines.test.ts` asserts the absence of that byte on purpose.
- **`create` force-removes first.** The name is deterministic, so a lingering exited
  container 409s the create on a name conflict — which is precisely the state a
  re-open needs to recover from.
- **Auth is checked before the session id.** An unauthenticated caller learns nothing
  about what the broker considers well-formed.

## `create` ensures the image; the daemon never does that for you

`docker.createContainer` — unlike the `docker run` CLI — does not implicitly pull a
missing image; the daemon just answers `create` with a 404 "No such image". A host
that never ran `codercom/code-server` (a fresh deployment, or an operator who bumped
`MEWBO_IDE_IMAGE`) failed **every** launch that way, for every session, regardless of
which project it named — indistinguishable from a genuine daemon outage because the
Python client collapses every non-`workspace_denied` broker error onto the same
`DockerUnavailable`/503.

`IdeContainers.ensureImage` closes this: an `inspect` first (cheap, no network once the
image is cached), a `pull` only when that 404s. It runs from two call sites for two
different reasons — `create` self-heals if the image was pruned after boot, and
`BrokerServer.ensureImage` runs once at startup, alongside `sweep`, so the very first
launch after a deploy doesn't pay a cold registry pull inline with a session open. Both
follow `sweep`'s never-throws contract: a registry hiccup at boot must delay readiness,
not refuse it, and `create`'s own call retries on the next launch either way.

## A missing bind source is not a missing volume subpath — they fail oppositely

A volume-backed workspace (`MEWBO_IDE_VOLUME_ROOTS`) mounts by `Type: "volume"` with a
`VolumeOptions.Subpath`, not by bind — see `resolveWorkspaceMount`. Do not reason from
the bind precedent above to this mount kind; verified live against the docker API, the
two behave **oppositely** on a missing source:

- A bind whose host-side source is missing is vivified as an empty directory on
  `start` (the deadline-file ordering above depends on exactly this).
- A volume subpath that does not yet exist inside the volume's data makes `start`
  fail outright — `create` succeeds, `start` 404s with "cannot access path ...: no
  such file or directory". No auto-vivification.

This is unreachable through the broker's own route, and the fix is NOT to make
`buildSpec`/`create` create the subpath — that would be the broker doing one more
privileged thing, the exact expansion the top rule of this file forbids. The
existence guarantee lives upstream, in two places, neither of which is this
package: `WorkspaceAllowlist.resolve`'s `realpath` refuses a not-yet-existing
`workspace_path` with a diagnostic `workspace_denied` (403) before `create` is ever
called, and the API's own callers guarantee existence at the point they hand a path
to the broker — the apps tier materializes its staging directory before returning
it, the wiki tier only returns a checkout it has confirmed on disk. If a caller
someday hands the broker a volume-backed path that does not yet exist, the allowlist
catches it first; a raw 502 from `create` would mean something upstream stopped
guaranteeing that.

## `remove` throws; `removeBestEffort` does not — both are needed

`create` depends on the force-remove having actually happened, so `remove` raises on
a daemon failure. `DELETE` must still unlink the deadline file and answer while the
daemon is down, or a docker outage strands the API holding state it can never clear.
The swallow is a second named method on `IdeContainers`, not a `try/catch` in the
route: which failures are survivable is policy, and routes are HTTP adapters that
make none.

## Status is never a 404

`GET /v1/ide/:sid` answers `absent` with a 200. A 404 would force the Python caller's
`is_running` check to distinguish "no container" from "the broker is broken", and the
cheap reading of that ambiguity treats both as "not running" — a fail-open that hides
an outage behind a normal-looking empty state.

## A response carries only what the caller cannot derive

`POST /v1/ide/:sid` and `.../extend` answer `{}`; status answers `{status}`, delete
answers `{removed}`. Nothing else. The container name and browser URL are derived
from the session id on BOTH sides, and the API owns `expires_at` authoritatively in
its own store; returning them adds a second channel for a fact that already has one,
and the two `expires_at` values come from two different clocks. The caller parses
every response with `extra="forbid"`, the mirror of this side's `.strict()`, so an
unread field must stay in lockstep across two languages and two containers for no
reader's benefit. Adding one means naming its consumer first.

**Corollary:** the API's `IdeContainerBackend` has ONE `teardown(session_id)` rather
than a container removal plus a deadline unlink, because `DELETE` here already does
both — split, a single stop costs two round trips whose second `removed` is always
`false`. Keep any new route whole the same way: if this service does two things
atomically, do not offer a way to ask for half of one.

## Error classification: 502 vs 503 is DIAGNOSTIC, not behavioural

`docker_unavailable` (503, `retryable: true`) means the daemon was never reached;
`docker_error` (502) means it answered and refused. **No consumer branches on the
difference** — `ide_broker.py` maps every code except `workspace_denied` onto the
same `DockerUnavailable`, and `apps/mewbo_api/tests/test_ide_broker.py` asserts both
produce the identical exception. The code buys a diagnosable log line, which
survives into the exception message; classify accurately for the operator reading
it, not because a caller will act on it.

`workspace_denied` is the ONE behavioural code: it becomes a `ValueError`, which
the extend route renders as a 400 naming the operator's misconfiguration instead of
hiding it behind "daemon unreachable".

Classification uses structured signals ONLY — a `statusCode` (the daemon answered)
and the errno set (it did not). Do not add a message-text heuristic under them: it
can only fire for an error carrying neither signal, and its outcome is
indistinguishable from the 502 arm to every consumer.

## Cost

Every route is `O(1)` — a bounded number of daemon round trips and at most one file
operation, independent of how many sessions or containers exist. Nothing here reads
a collection on a request path.

The one `O(collection)` operation is `sweep()`, which is bounded by the number of
web-ide containers and runs **once at boot**, never on a request path. It is also
the only long-ish operation, and it is explicitly allowed to fail: `BrokerServer.sweep`
never throws, so a daemon that is not up yet delays the reap, not the listener.

`create`'s `ensureImage` step is the other exception to the round-trip bound: on a
cached image it's one cheap `inspect`, but on a cold one it's a real network pull —
sized by the registry, not by anything this service controls. `BrokerServer.ensureImage`
pays that cost once at boot, alongside `sweep`, so it is ordinarily absorbed before the
first request rather than inline with a session open.

There are no streaming or long-lived routes, so this service spends no concurrency
budget of the kind `apps/mewbo_api/CLAUDE.md` describes. **Keep it that way** — if a
log-follow or exec-attach route is ever proposed here, it needs a stated concurrency
bound first, because Fastify's single event loop is this process's whole capacity.

## Code shape

| Class | Owns |
|---|---|
| `BrokerConfig` | the validated env surface, plus `memoryBytes()`/`nanoCpus()`/`parseRoots()` |
| `WorkspaceAllowlist` | the roots and the one `resolve()` decision |
| `DeadlineFiles` | the state dir; write/read/clear |
| `IdeContainers` | the docker handle, spec CONSTRUCTION, create/inspect/remove, the sweep, `ensureImage` |
| `IdeRoutes` | HTTP adaptation only — validate, delegate, serialize |
| `BrokerServer` | composition root, the one error-rendering seam, process lifecycle |
| `BrokerError` | every refusal: its status, wire code, retryability and body |

- **The clock is injected** (`now: () => Date`) into `IdeContainers` and `IdeRoutes`,
  so expiry and sweep tests assert exact timestamps with no sleeping and no fake
  timers.
- **`BrokerServer.create` takes ONE varying collaborator, the docker client.**
  Production passes dockerode; a test passes `FakeDockerClient`. Everything else is
  derived from `BrokerConfig`, so there is one composition path, not two.
- **`any` appears exactly once**, at `index.ts`'s dockerode boundary, narrowed
  immediately to the `DockerClientLike` interface that declares the four calls this
  service actually makes. Nothing downstream sees the raw client.
- **`BrokerError` is the import-cycle cure** — refusals from the allowlist, the
  container controller and the routes all render through one hook.

## Dependencies

`fastify` · `dockerode` · `zod`. `zod` is pinned to `^3.25.76`, the same range the
console carries, so the house dependency stays single-versioned.

⚠️ **`dockerode` is held at `^4.0.12` despite a moderate advisory.** dockerode 4
depends on `uuid` <11.1.1 (GHSA-w5hq-g745-h8pq: a buffer bounds check that fires
only when the caller supplies `buf`, which dockerode never does — unreachable here).
dockerode 5 clears the advisory but ships **no type declarations** while
`@types/dockerode` still tracks 4.x, and a v5 runtime under v4 declarations is
silent drift on a privileged component. Revisit when `@types/dockerode` ships a v5
line or dockerode publishes its own types; the migration is small, since the whole
surface used here is `new Docker({socketPath})` plus the four `DockerClientLike`
calls.

## Testing

`vitest`, no live daemon anywhere.

- **The symlink-escape test creates a REAL symlink in a tmpdir** — a mocked
  `realpath` would only prove the mock. It first asserts the link's own path is
  textually under the root, which is what demonstrates a string prefix test passes it.
- **`FakeDockerClient` records calls, not just returns.** The sweep's label filter is
  provable only by inspecting the arguments the broker sent (`listCalls`).
- **`mewbo-ide-proxy` appears by name in the sweep tests** — it is the real container
  a name-prefix sweep destroys, so the test names the actual casualty.
- **The `.strict()` test smuggles `image`, not a nonsense key** — it asserts that a
  spec fragment is refused, not that Zod works.
