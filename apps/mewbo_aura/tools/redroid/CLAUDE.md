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
- **Both services are resource-capped, and the caps are load-bearing.** An uncapped redroid is
  `privileged` with no cgroup limit, so it inherits the whole host: a runaway inside Android
  (software-GPU render storm, boot loop, leaking app) swaps the workstation to death instead of
  dying itself — this took the host down once. `memswap_limit == mem_limit` is the half that
  actually saves you: it denies the container ANY swap, so an overrun becomes a fast in-container
  OOM (Android's lowmemorykiller reaps it) rather than host-wide swap thrash. Caps are sized from
  measured idle (AOSP ~1.1G / GMS ~1.6G, ~1000 PIDs each) plus headroom. Raise one only against an
  observed in-container OOM, never "to be safe" — an unbounded container is the bug.
- **The GMS device is a reference-capture rig, not just an overlay prop.** `adb ... uiautomator
  dump` on Google's own surfaces yields their *resource-ids*, which name their internal component
  architecture outright (that's how the action-card anatomy in `DESIGN.md` §6 was derived — Google
  literally names it `assistant_robin_action_card_{entity_header,entity_contents}`). Pair it with
  `exec-out screencap` + pixel-sampling for tone and `wm density` for the px→dp divisor. Reach for
  this before hand-eyeballing a reference from screenshots. Note the Gemini launcher activity is a
  thin shell that redirects into `com.google.android.googlequicksearchbox` — resolve the launch
  intent, don't assume the package you started is the one you're now looking at.

See `apps/mewbo_aura/CLAUDE.md`'s device matrix table for the Tier-1-vs-Tier-2 (physical Pixel)
split and the build→install→verify loop itself; this file is ops-only for the container.
