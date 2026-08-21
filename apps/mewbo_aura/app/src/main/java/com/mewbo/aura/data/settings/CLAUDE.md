> ↑ [data/CLAUDE.md](../CLAUDE.md) · [apps/mewbo_aura/CLAUDE.md](../../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../../CLAUDE.md)

# Aura Settings Store — data/settings/

Scope: `data/settings/` — `SettingsStore` (DataStore-preferences, file-scoped `aura_settings`) and
`KeystoreCipher`. Exposes only `Flow`s; the blocking `first()` reads happen at the SEAMS that cannot
suspend (`AuthInterceptor`/`BaseUrlInterceptor` on OkHttp's chain, `SessionStreamClient`,
`DeviceToolGate`), never here.

## Keys whose DEFAULT is load-bearing

- `base_url` → **default `""`** — forces onboarding through Settings; never guess a host.
- `api_key_ciphertext` + `api_key_iv` → the two halves of the encrypted key (below). Missing EITHER
  ⇒ `null`.
- `selected_model` → the full-app default (bare/unprefixed; also written by the chat top-bar picker).
  `overlay_default_model` → the independent assist-overlay default, read only at the overlay's
  session-creation seam. Both blank ⇒ the API's configured default, so a voice trigger and the app can
  run different models.
- `disabled_device_tool_ids` (a `stringSet`) → **ABSENT means the screen-control defaults, not "nothing
  disabled".** The nine handoff-style tools stay enabled; the three screen-control tools start OFF
  (`DeviceToolToggles.DEFAULT_DISABLED_TOOL_IDS`), because they drive the phone rather than hand a
  request to a system app. A STORED set is authoritative — the fallback applies only to a missing key,
  so re-enabling a control tool is not undone on the next read. **The setter reads the same default**,
  or the first toggle would persist an empty set and silently enable the other two. Read through
  `DeviceToolGate` ([`data/device/CLAUDE.md`](../device/CLAUDE.md)).
- `streamlit_widgets_enabled` → **default ON.** Gates BOTH the `stlite` capability header AND
  `widget_ready` rendering at the same seam; OFF is an escape hatch to a plain chat client.
- `selected_project` → default `""` = the Temporary temp-dir cwd; the value is a
  `ProjectSummary.contextKey`.
- `speech_volume_boost_db` → **default `0` (off), and stored UNCLAMPED on purpose.** The range
  belongs to the effect, so `voice/SpeechVolumeBoost` owns the clamp and this layer only persists
  what it was handed — clamping in both places is how two ranges drift, and `data/` may not import
  `voice/` to share the constant. Off is the default because a boost amplifies past what the
  platform itself will do. Read through `SpeechVolumeBoostGate`, never here.
- `speak_responses` (`true`), `reduced_motion` (`false`), `voice_use_fakes`, `display_name`.

## API key at rest — Keystore AES/GCM, decrypt off-main

`KeystoreCipher` wraps the key with a hardware-backed AndroidKeystore AES/GCM/NoPadding 256-bit key
(alias `mewbo_aura_api_key`), storing Base64 ciphertext + a per-encrypt random IV in DataStore.

**The `apiKey: Flow<String?>` decrypt is pinned to `Dispatchers.IO` via `.flowOn`** —
`KeystoreCipher.decrypt` is a synchronous Keystore/TEE IPC call, and without it the `.map{}` runs on
the collector's dispatcher, usually Main. A decrypt failure degrades to `null`, never a throw.

Deliberately NOT `EncryptedSharedPreferences`: settings already live in DataStore, so one AES/GCM
helper beats a second storage mechanism for a single field.

**A 401 on the dev device almost always means this stored key is GONE** (container recreated, `data/`
wiped, fresh install), not an auth bug — re-seed via the app-root CLAUDE.md's Recurring-401 recipe.

## 🚨 ONE `DataStore` per preferences file — naming the same file does not share it

`preferencesDataStore(name = …)` **constructs** a store. A second delegate naming a file that
already has one creates a SECOND live instance, and DataStore refuses that outright:

```
IllegalStateException: There are multiple DataStores active for the same file:
  /data/data/com.mewbo.aura/files/datastore/aura_settings.preferences_pb
```

It throws on first read, on the main thread, and the app dies at launch. This shipped: the debug-only
`voice/VoiceBackends` declared its own `aura_settings` delegate to read the same key `SettingsStore`
writes — the intent was to stop using a second file, and the implementation created a second STORE
instead.

**Every gate in this repo is structurally blind to it.** It compiles, lint passes, the whole unit
suite is green, and it launches perfectly on redroid — because that reader sits behind
`isEmulator && !isRecognitionAvailable`, which short-circuits on an emulator, so only real hardware
ever takes the arm that opens the duplicate. The first witness was a phone.

- **A reader in another package goes through a narrow seam bound from the ONE owner**, never through
  a delegate of its own. `VoiceFakesGate` (bound in the debug `di/VoiceModule` from
  `SettingsStore.voiceUseFakes`) is the shape, and it matches `SpeechModule.provideSpeechEngineGate`.
  A seam makes a second owner impossible; matching the file name only makes it invisible.
- **`DataStoreOwnershipTest` is the guard** — it scans the sources and fails if any preferences file
  is declared by more than one delegate. A source scan rather than a Robolectric test on purpose:
  two live stores need a real component graph on a real device to exist at all, so no JVM test can
  observe the crash itself. Proven able to fail before being kept.
