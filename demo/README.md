# Demo-as-code

Every screenshot in `docs/assets/img/mewbo-{console,wiki,search}-*` is a **rendered
artifact**: a versioned capture flow executed against a seeded, fully containerized
demo stack — the full console **Tasks**, **Wiki**, and **Search** surfaces (20
images). Nobody hand-captures screenshots; when the UI changes, the flows are
re-run and the artifacts are overwritten in place. A broken flow **is** the
staleness signal.

This directory implements the local screenshot PoC; Android capture, video
rendering, and CI wiring are later phases of the same effort.

## Quickstart

```sh
make demo            # up → seed → capture, end to end
# or step by step:
make demo-up         # build (first run) + boot mongo/api/console, wait healthy
make demo-seed       # rebase + write the fixture bundle through store contracts
make demo-shots-web  # run the Playwright flows, overwrite docs/assets/img/*
make demo-down       # tear down (removes volumes — the stack is ephemeral)
```

Prerequisites: Docker with the compose plugin. First `demo-up` builds
`mewbo-api:demo` / `mewbo-console:demo` from the local Dockerfiles (never touching
the deployed `ghcr.io/...:latest` tags) and pulls the pinned Playwright image.

The console is published loopback-only for humans at
`http://127.0.0.1:3210` (`DEMO_CONSOLE_PORT`); everything else stays inside the
isolated `mewbo-demo` bridge network.

## Anatomy

| Piece | Where | What it does |
|---|---|---|
| Compose stack | `demo/docker-compose.demo.yml` | mongo + api + console on an isolated bridge network, plus profile-gated one-shot `seed` and `shots` services |
| Stack env | `demo/demo.env` | `DEMO_T0`, demo API key, db name, debug port — committed, no secrets |
| API config | `demo/configs/app.json` | minimal deterministic `AppConfig`: mongodb storage pointed at the demo mongo (`mongo:27017`/`mewbo_demo`), `scg.enabled=true`, no LLM key, marketplaces emptied. Mounted into api AND seed |
| nginx override | `demo/nginx-console.demo.conf` | copy of `docker/nginx-console.conf` with `proxy_pass` retargeted to `api:5125` for the bridge network |
| Seeders | `demo/seeder/` | three atomic seeder classes writing through real store contracts: `DemoSeeder` (sessions/triggers), `WikiSeeder` (projects/pages/graph/jobs/Q&A), `SearchSeeder` (workspaces/runs/SCG) |
| Fixture bundles | `demo/seeder/bundles/{console,wiki,search}-poc.json` | the seeded world per surface: timestamps as offsets from T0, `extra="forbid"` schema-validated |
| Capture flows | `apps/mewbo_console/tests/demo/` + `playwright.demo.config.ts` | one spec per artifact (console + `wiki-*` + `search-*`); `shots.ts` is the single manifest of output paths, `fixtures.ts` the shared frozen-clock + capture helpers |

## Determinism rules

1. **Frozen time.** `DEMO_T0` (in `demo/demo.env`) is the one true "now". The
   seeder rebases every fixture offset from it; the flows freeze the browser clock
   at it (`page.clock.install`). Relative timestamps ("45 minutes ago") therefore
   render identically forever. Never write an absolute timestamp into a bundle.
2. **Ephemeral state.** The demo mongo has no volume. Every `demo-up` starts
   empty; `demo-seed` recreates the world byte-identically. There is nothing to
   migrate and nothing to drift.
3. **No outbound network.** The stack is bridge-isolated with no LLM key; seeded
   sessions are *completed*, so nothing ever calls a model. Replay only.
4. **Pinned renderer.** The `shots` service uses the
   `mcr.microsoft.com/playwright` image whose version exactly matches
   `@playwright/test` in `apps/mewbo_console/package-lock.json` — same browser
   build everywhere, no local-Chrome pixel drift.
5. **Stub outbound / racy loads.** Anything that would race the capture or reach
   off-network is `page.route`-stubbed: the wiki badge SVG (an external CDN image)
   to a committed fixture, and the wiki branches `ls-remote` + freshness calls to
   deterministic responses — so no shot depends on the network.
6. **Acceptance = zero diff.** Running seed + capture twice in a row must produce
   no `git diff` in `docs/assets/img/` — all 20 shots are byte-identical. If any
   drifts, something nondeterministic crept in — fix the cause, never hand-edit an
   artifact. (The two force-directed graph screenshots were removed; the sparse
   WebGL capability graph was the one shot that could not be made byte-stable.)

## Adding a new screenshot

1. Extend `demo/seeder/bundles/console-poc.json` with whatever seeded state the
   screen needs (through the existing models — the bundle is schema-validated).
2. Add the output path to `apps/mewbo_console/tests/demo/shots.ts` (the only
   place paths live) and a spec next to the existing ones.
3. `make demo-up demo-seed demo-shots-web`, eyeball the artifact, run it twice,
   commit the new baseline.

## Debugging

- Console UI: `http://127.0.0.1:3210` (already seeded once `demo-seed` ran).
- API through the proxy: `curl -H "X-Api-Key: mewbo-demo-master-token" http://127.0.0.1:3210/api/sessions`.
- Mongo is *not* published on the host; `docker compose -f demo/docker-compose.demo.yml --env-file demo/demo.env exec mongo mongosh mewbo_demo`.
- Flow traces/results land in the Playwright `outputDir` (gitignored), never in `docs/`.
