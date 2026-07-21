> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Settings Store — data/settings/

Scope: `data/settings/` — `SettingsStore` (DataStore-preferences, file-scoped `aura_settings`) and
`KeystoreCipher`. Exposes only `Flow`s; the blocking `first()` reads happen at the SEAMS that can't
suspend (`AuthInterceptor`/`BaseUrlInterceptor` on OkHttp's chain, `SessionStreamClient`,
`DeviceToolGate`), never here.

## Keys, and the defaults that are load-bearing

- `base_url` → **default `""` (empty by design)** — forces onboarding through Settings, no host guess.
- `api_key_ciphertext` + `api_key_iv` → the two halves of the encrypted key (below).
- `selected_model` → the FULL-APP default (bare/unprefixed; also written by the chat top-bar picker);
  `overlay_default_model` → the independent ASSIST-OVERLAY default, read only at the
  overlay's session-creation seam. Both blank ⇒ the API's configured default. A voice trigger and the
  app can run different models.
- `disabled_device_tool_ids` (a `stringSet`) → **empty default = every `device_*` tool ENABLED**
  (preserves pre-toggle behavior). Read through `DeviceToolGate`
  ([`data/device/CLAUDE.md`](../device/CLAUDE.md)).
- `streamlit_widgets_enabled` → **default ON** (renderer exists). Gates BOTH the `stlite`
  capability header AND `widget_ready` rendering at the same seam; OFF = a plain chat client (an escape
  hatch, not the default).
- `selected_project` → default `""` = the Temporary temp-dir cwd; value is a `ProjectSummary.contextKey`.
- `speak_responses` (default `true`), `reduced_motion` (default `false`), `voice_use_fakes`, `display_name`.

## API key at rest — Keystore AES/GCM, decrypt off-main

`KeystoreCipher` wraps the key with a hardware-backed AndroidKeystore AES/GCM/NoPadding 256-bit key
(alias `mewbo_aura_api_key`), storing Base64 ciphertext + a per-encrypt random IV in DataStore. The
`apiKey: Flow<String?>` decrypt is pinned to `Dispatchers.IO` via `.flowOn` — `KeystoreCipher.decrypt`
is a synchronous Keystore/TEE IPC call and the `.map{}` otherwise runs on the collector's dispatcher
(often Main). Missing ciphertext OR iv, or a decrypt failure, ⇒ `null`. Deliberately NOT
`EncryptedSharedPreferences` — settings already live in DataStore, so one AES/GCM helper beats a second
storage mechanism for a single field (KISS).

**A 401 on the dev device almost always means this stored key is GONE** (container recreated / `data/`
wiped / fresh install), not an auth bug — re-seed deterministically (app-root CLAUDE.md § "Recurring-401").
