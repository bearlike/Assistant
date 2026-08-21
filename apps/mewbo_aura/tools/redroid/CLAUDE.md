> ↑ [apps/mewbo_aura/CLAUDE.md](../../CLAUDE.md) · [root](../../../../CLAUDE.md)

# redroid Dev Container — tools/redroid/

Scope: `tools/redroid/` — the redroid Docker Compose setup (`docker-compose.yml`,
`build-gms-image.sh`), the primary Tier-1 dev device (AOSP-only, no `SpeechRecognizer`/TTS, software
GPU; see the device matrix in [`apps/mewbo_aura/CLAUDE.md`](../../CLAUDE.md)). Hard-won ops facts —
don't re-derive any of these:

- **The host binder module is NOT loaded by default** and `sudo` may be unavailable. Load it via a
  privileged helper container:
  `docker run --rm --privileged -v /lib/modules:/lib/modules:ro busybox modprobe binder_linux devices=binder,hwbinder,vndbinder`.
- **The compose file pins an explicit bridge subnet** because a dev host's default Docker address
  pools are commonly already fully allocated. Don't remove that block; change the value in
  `docker-compose.yml` if it collides on your host.
- **`data/` is written by container root** — wipe it with
  `docker run --rm -v "$PWD/data:/wipe" busybox sh -c 'rm -rf /wipe/*'`, never host `rm -rf`.
- **Backend line-of-sight**: the container is dual-homed (its own bridge + the deployed stack's
  `assistant_default`), and Android's DNS is Docker's embedded resolver
  (`androidboot.redroid_net_dns1=127.0.0.11`) — so inside the app the dev base URL is
  **`http://api:5125`** (compose service name, direct container path; cleartext allowed by the
  `enterprise` flavor's `network_security_config`, so build `assembleEnterpriseDebug` for redroid
  dev). LAN hostnames configured on your network also resolve via the host's upstream. Don't point
  dev at a self-signed public URL.
- **GMS variant** (the stock GMS assistant app as an assistant-role/overlay UX reference): bake once
  with `tools/redroid/build-gms-image.sh`, start with `docker compose --profile gms up -d` (adb
  `localhost:5556`, state in `data-gms/`). First-boot setup wizard black-screens on the software GPU
  — the skip commands + account sign-in (GSF-ID registration) steps are in the `redroid-gms` block of
  `docker-compose.yml`. MindTheGapps ships its assistant app pre-set as default; no mic in redroid, so
  typed/visual assistant interaction only.
- **Both services are resource-capped, and the caps are load-bearing.** An uncapped redroid is
  `privileged` with no cgroup limit, so it inherits the whole host: a runaway inside Android
  (software-GPU render storm, boot loop, leaking app) swaps the workstation to death instead of dying
  itself — this took a host down once. `memswap_limit == mem_limit` is the half that actually saves
  you: it denies the container ANY swap, so an overrun becomes a fast in-container OOM (Android's
  lowmemorykiller reaps it) rather than host-wide swap thrash. Caps are sized from measured idle
  (AOSP ~1.1G / GMS ~1.6G, ~1000 PIDs each) plus headroom. Raise one only against an observed
  in-container OOM, never "to be safe" — an unbounded container is the bug.
- **The GMS device is a reference-capture rig, not just an overlay prop.** `adb … uiautomator dump` on
  Google's own surfaces yields their *resource-ids*, which name their internal component architecture
  outright (that is how the action-card anatomy in `DESIGN.md` was derived). Pair it with
  `exec-out screencap` + pixel-sampling for tone and `wm density` for the px→dp divisor. Reach for
  this before hand-eyeballing a reference from screenshots. Note the Gemini launcher activity is a
  thin shell that redirects into `com.google.android.googlequicksearchbox` — resolve the launch
  intent, don't assume the package you started is the one you're now looking at.

## TV geometry — what this container can and cannot witness

Boot at 16:9 by overriding the geometry params already in `docker-compose.yml`:
`androidboot.redroid_width=1920 androidboot.redroid_height=1080 androidboot.redroid_dpi=213`
(≈960×540dp landscape; the exact TV density is a judgement, the geometry is the point).

- **D-pad traversal IS witnessable here, unmodified.** `adb shell input keyevent 19/20/21/22/23` is
  dispatched by the input framework and Compose focus responds to key events — neither depends on
  `characteristics=tv` nor on the leanback feature. This is the cheap tier for "is every control
  reachable by a remote".
- **`android.software.leanback` is NOT, and cannot be added.** It is declared by a permissions XML
  baked into the image at build time and `/system` is read-only. So this container can never
  witness store filtering, the TV launcher row, banner presentation, or any code reading
  `hasSystemFeature(FEATURE_LEANBACK)` — those need a real Google TV image. A GitHub issue search
  across `remote-android/redroid-doc` for tv/leanback/characteristics returned nothing; rebuilding
  from AOSP source is the only documented lever and is not worth it.

The Tier-1-vs-Tier-2 split and the build→install→verify loop itself live in
[`apps/mewbo_aura/CLAUDE.md`](../../CLAUDE.md); this file is ops-only for the container.
