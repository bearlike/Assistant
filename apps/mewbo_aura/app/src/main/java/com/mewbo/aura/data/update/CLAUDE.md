> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura In-App Updater — data/update/

Scope: `data/update/` — the release client, the version arithmetic, the download-and-verify
pipeline, and the platform install seam. The UI over it is
[`ui/settings/`](../../ui/settings/CLAUDE.md)'s **About app** section; the wiring is
[`di/UpdateModule`](../../di/CLAUDE.md).

The whole point: **the user updates the app without ever leaving it.** Detect a newer release, pick
the file that fits THIS device, fetch it with visible progress, prove it is what it claims, install
it. No browser, no file manager, no releases page — which matters most on a television, where none
of those three exist.

## One client, two forges — the load-bearing fact

**Gitea's release API is GitHub-shaped.** Measured field-for-field against a live Gitea instance and
against api.github.com: `tag_name`, `name`, `body`, `draft`, `prerelease`, `published_at`, and
`assets[]` with `name`, `size`, `browser_download_url` are spelled identically. So there is ONE DTO,
ONE Retrofit interface, ONE repository, and **no `if (isEnterprise)` anywhere in the client.**

The only difference is the API root, and it is a BUILD constant (`UpdateChannel`, stamped from
`BuildConfig` in `di/`) rather than a runtime setting. Two reasons, both load-bearing: a shipped APK
must not be re-pointable at another forge, and a private forge's hostname may never appear in a
tracked file. The public flavor's `https://api.github.com/` is a tracked default because it is a
public fact; the enterprise root arrives as a Gradle argument the same way the enterprise CA does
(`-Pmewbo.updateApiRoot`, `AURA_UPDATE_API_ROOT`, or `~/temp_folder/aura-update-api-root.txt`), and
`requireEnterpriseUpdateApiRoot` fails an enterprise build that has none.

**Page size is sent under BOTH spellings on every request** — `per_page` (GitHub) and `limit`
(Gitea). Each forge reads its own and ignores the other's, verified against both. One request shape,
no branch.

## `/releases/latest` is the WRONG endpoint, and this was measured

It fails on this repository in two independent ways, either of which alone rules it out:

- **It excludes prereleases on both forges.** Aura's newest build is routinely published as one, so
  a device already running `aura-0.0.20.0` is told the latest release is `aura-0.0.19.0`. Verified
  live: `/releases/latest` returned `0.0.19.0` while `0.0.20.0` sat at the top of the list.
- **The tag namespace is SHARED with the server's own releases** (`v0.0.12` and friends). "The
  latest release" is not "the latest Aura build", and a release with no APK at all can be the one
  that endpoint returns.

So the client lists one bounded page and chooses on the client. `UpdateChannel.TAG_PREFIX`
(`aura-`) is the discriminator — a tag without it is dropped rather than parsed, because `v0.0.12`
would otherwise read as a perfectly plausible version number for this app.

## Release-asset nomenclature

**`aura-<versionName>-<flavor>-<buildType>.apk`** — e.g. `aura-0.0.20-enterprise-debug.apk`. Emitted
by the build (`app/build.gradle.kts`, `variant.outputs.outputFileName`), never typed by hand at
release time, and derived from the single `auraVersionName` so a version bump renames the artifact
by itself.

- The version is the BASE name, not the variant's: the enterprise flavor appends `-enterprise` to
  `versionName`, and the flavor is already its own segment.
- **No ABI segment**, deliberately — this app ships one universal APK. Check `splits`/`abiFilters`
  before adding a dimension that does not exist.
- **The picker matches on the SUFFIX, not the whole name**, and that is what keeps every
  already-published release visible: the old scheme was `app-<flavor>-<buildType>.apk`, which ends
  identically. The two segments in the suffix are exactly the two that decide whether a file will
  work here — the flavor (whether the APK trusts the deployment's own CA) and the build type (which
  key signed it).

## Version arithmetic — two rules, both traps

A release tag is `aura-0.0.20.0` (four segments, the last a re-release counter). The installed
`versionName` is `0.0.20`, or `0.0.20-enterprise` on that flavor.

- **Everything from the first non-numeric character is dropped**, which is what makes the flavor
  suffix invisible rather than a special case at each reader. Without it an enterprise build reads
  as perpetually out of date.
- **The shorter side is zero-PADDED, never truncated.** `0.0.20` == `0.0.20.0`, so a release re-cut
  as `0.0.20.1` is correctly newer — which is the only reason a re-release counter exists.

`AppVersion.parse` is TOTAL: a tag nobody planned for, or a segment too large for an `Int`, returns
`null` and the release is skipped. It must never throw on a screen the user is looking at.

## Verification — three checks, because there is no checksum to have

**Neither forge exposes a digest for a release asset.** Verified on both: Gitea's asset object is
`{id, name, size, download_count, created_at, uuid, browser_download_url}` and nothing more. So the
declared byte count plus the APK's own manifest are the whole integrity story, and that is a limit
of the wire format rather than a choice.

1. Declared `size` vs bytes on disk — catches a truncated transfer.
2. The archive's own `packageName` vs the installed one — catches a file that is not this app.
3. The archive's `versionCode` vs the installed one — catches a stale artifact that would install as
   a downgrade.

**A wrong-FLAVOR APK cannot be caught here and never will be**: both flavors share one
`applicationId`, so the archive's manifest is identical. It is caught earlier, by the asset name —
which is precisely why the naming scheme carries the flavor.

Bytes land on a `.part` file renamed only once all three pass, and the download directory is cleared
at the START of every attempt, so a transfer killed mid-flight cannot leave something a later run
mistakes for a finished download.

## Install — `PackageInstaller`, and therefore no `FileProvider`

The session API is used rather than `ACTION_VIEW`/`ACTION_INSTALL_PACKAGE`: it keeps the user inside
the app's own flow and reports a real, discriminated result back instead of a fire-and-forget
intent. **A consequence worth knowing before anyone "adds the missing FileProvider": there is none
to add.** The session streams bytes directly from our own process, so no content URI is shared with
anyone and no provider is required. A `FileProvider` is only needed by the legacy intent route.

`REQUEST_INSTALL_PACKAGES` is a SPECIAL, app-op-backed grant — the manifest declaration is not the
grant. It is read with `PackageManager.canRequestPackageInstalls()`, granted on a system screen, and
has **no dialog and therefore no result callback**, exactly like `SYSTEM_ALERT_WINDOW`; the About
section re-reads it on RESUME for the same reason the overlay row does.

**Reachability of that screen is answered at RUNTIME, not by a `DeviceShape` member.** A member
would have to state a value for `Television`, and nobody has run this on a Fire TV or Android TV —
an unmeasured member is a guess wearing a type. `ApkInstaller.openInstallPermissionScreen()` returns
`false` when nothing resolves, and the row says so, which is the same posture
`PermissionRequest.openOverlaySettings` already takes.

## 🚨 Signature reality — the key, not the machine, decides self-update

**Android refuses to update an installed app across a signature change.** The install fails with
`STATUS_FAILURE_CONFLICT` and the only cure is an uninstall, which loses the app's data.

Every published Aura release is an `enterpriseDebug` build. `app/build.gradle.kts` gives the debug
build type the same `release` signing config as release, so a configured keystore
(`AURA_KEYSTORE_B64` and its credentials) signs every variant with one stable key. A release cut on
any machine then chains onto an installed release cut elsewhere.

When that keystore is unset, the signing config deliberately falls back to AGP's auto-generated
`~/.android/debug.keystore`, which is created per MACHINE and not shared. Then:

- Releases built on the SAME machine chain correctly, and self-update works.
- A release cut on any other machine is signed with a different key, and every device that installed
  a previous build will refuse it. Nothing warns at build time; the failure appears only on a user's
  device, at install.
- Cross-FLAVOR is fine (`public` ↔ `enterprise` share one signing config source and one
  `applicationId`); cross-MACHINE is not.

`InstallOutcome.SignatureMismatch` names the cause and the remedy in a sentence rather than
surfacing a status number.

## Layering

`data/update/` imports nothing from `ui/`, same as the rest of `data/`. The two Android-facing
concerns are behind seams declared HERE and bound in `di/` — `PackageFacts` (the two
`PackageManager` reads) and `PlatformInstaller` (implemented by `ApkInstaller`) — which is what
keeps `AppUpdateRepository`, where every rule worth testing lives, a plain-JVM class with no
`Context` and no Robolectric.

**`AppUpdateRepository` is `@Singleton` and runs on `@ApplicationScope`, not on a ViewModel.** The
artifact is around a hundred megabytes; a download owned by the Settings screen would be cancelled
by the user backing out for ten seconds and would restart from zero on the way back in.

## The states, and the three collapses they refuse

`AppUpdateState` has more arms than "checking / up to date / available" because each collapse is a
claim nobody measured — the same law `ui/settings/CLAUDE.md` states for permissions:

- **`CheckFailed` is not `UpToDate`.** An unreachable forge means nobody asked.
- **`NoInstallableBuild` is not `UpToDate` and not a failure.** A newer release exists and publishes
  no file this device can install. **This is the measured state of the public GitHub mirror**, whose
  releases carry no APK assets at all — so the `public` flavor's check legitimately finds a newer
  release with nothing in it, and must say that rather than either lie.
- **`Unsupported` is not `UpToDate`.** A build given no release source never looked anywhere.
