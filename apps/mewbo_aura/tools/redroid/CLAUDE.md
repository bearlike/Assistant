> ↑ [apps/mewbo_aura/CLAUDE.md](../../CLAUDE.md) · [root](../../../../CLAUDE.md)

# redroid Dev Container — tools/redroid/

Scope: `tools/redroid/` — the redroid Docker Compose setup (`docker-compose.yml`,
`build-gms-image.sh`), the primary Tier-1 dev device (`apps/mewbo_aura/CLAUDE.md`'s device matrix:
UI/chat/streaming/reducer work; AOSP-only, no `SpeechRecognizer`/TTS, software GPU). Hard-won ops
facts — don't re-derive any of these:

- **Host binder module is NOT loaded by default** and `sudo` may be unavailable. Load it via a
  privileged helper container:
  `docker run --rm --privileged -v /lib/modules:/lib/modules:ro busybox modprobe binder_linux devices=binder,hwbinder,vndbinder`.
- **This host's Docker address pools are exhausted** → the compose file pins subnet
  `10.213.99.0/24`. Don't remove that block.
- **`data/` is written by container root**; wipe it with
  `docker run --rm -v "$PWD/data:/wipe" busybox sh -c 'rm -rf /wipe/*'`, not host `rm -rf`.
- **Backend line-of-sight**: the container is dual-homed (its own bridge + the deployed stack's
  `assistant_default`), and Android's DNS is Docker's embedded resolver
  (`androidboot.redroid_net_dns1=127.0.0.11`) — so inside the app the dev base URL is
  **`http://api:5125`** (compose service name, direct container path; cleartext allowed by the
  `enterprise` flavor's `network_security_config` — build `assembleEnterpriseDebug` for redroid dev).
  LAN hostnames configured on your network also resolve via the host's
  upstream. Don't point dev at the self-signed public URL.
- **GMS variant** (the stock GMS assistant app as assistant-role/overlay UX reference): bake once
  with `tools/redroid/build-gms-image.sh`, start with `docker compose --profile gms up -d` (adb
  `localhost:5556`, state in `data-gms/`). First-boot setup wizard black-screens on the software
  GPU — skip commands + account sign-in (GSF-ID registration) steps are in the `redroid-gms` block
  of `docker-compose.yml`. MindTheGapps ships its assistant app pre-set as default; no mic in
  redroid → typed/visual assistant interaction only.

See `apps/mewbo_aura/CLAUDE.md`'s device matrix table for the Tier-1-vs-Tier-2 (physical Pixel)
split and the build→install→verify loop itself; this file is ops-only for the container.
