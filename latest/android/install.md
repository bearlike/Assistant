# Install

You install Aura from an APK. You can download a prebuilt APK from the project's releases, or build one yourself from source. Either way, the last step is the same: point the app at your Mewbo server.

## Get the APK {#get-apk}

### Download a release {#download}

Prebuilt APKs are published on the project's [GitHub releases](https://github.com/bearlike/Assistant/releases/latest). Open the latest release, download the `.apk` asset attached to it, copy it to your phone, and open it. Android will ask you to allow installing apps from this source the first time. The build is self-signed, so accept the prompt to continue.

> [!NOTE] Prerelease builds
> These are debug-signed prereleases, not Play Store releases. They install directly from the APK file. Update by installing a newer APK over the top. Your server connection and chats are preserved across an update.

### Build from source {#build}

Aura lives in the Mewbo repository as its own self-contained Gradle project under `apps/mewbo_aura`. To build it you need the Android SDK installed, with `ANDROID_HOME` pointing at it.

From the `apps/mewbo_aura` directory, build the public flavor:

```bash
./gradlew :app:assemblePublicDebug
```

The APK lands at `app/build/outputs/apk/public/debug/app-public-debug.apk`. Install it on a connected device with `adb`:

```bash
adb install -r app/build/outputs/apk/public/debug/app-public-debug.apk
```

## Choose a build flavor {#flavors}

Aura builds in two flavors. They differ only in which TLS certificates the app trusts.

| Flavor | Trusts | Use it when |
|--------|--------|-------------|
| **public** | Standard system certificate authorities only | Your server presents a certificate signed by a public certificate authority. This is the default, and the only flavor built by continuous integration. |
| **enterprise** | System authorities plus a private certificate authority you provide | Your organization runs a self-hosted server behind a certificate from its own internal certificate authority, or reaches it over a local network without TLS. |

Most people want the **public** flavor. Choose **enterprise** only if your organization's self-hosted server sits behind a certificate that a stock Android device would not trust on its own. The enterprise flavor bundles a private root certificate at build time so the app can validate that connection. That certificate is supplied by whoever builds the app for the organization, never hardcoded into the public build.

> [!TIP] Not sure which flavor?
> If your Mewbo server is reachable at an `https://` address that a browser trusts without a warning, use **public**. If your phone's browser shows a certificate warning for the same address, your organization needs **enterprise** with its own certificate authority added to the build.

## Connect to your server {#connect}

On first launch there is no server configured yet. The chat greeting reads "No backend configured, open Settings." Open Settings from the gear icon in the navigation drawer.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-aura-03-settings.png" alt="Aura's Settings screen showing the Server base URL field and a masked API key field, with a Validate & save button" style="width: 100%; max-width: 360px; height: auto;" />
</div>

Settings has two connection fields:

- **Server base URL:** the address of your Mewbo API server, for example `https://mewbo.example.com`.
- **API key:** the key you issued from the console. It is entered as a masked field.

Tap **Validate & save**. Before it stores anything, Aura makes a live request to your server to confirm the URL and key work. On success you see a short confirmation with the number of models your server offers, and both fields are saved. On failure nothing is saved, and an inline message tells you what went wrong (an invalid key, an unreachable host, a timed-out connection, or a malformed URL).

If you want to save the values without the check (for example, to configure the app before the server is up), the error message offers a **Save anyway** option.

> [!NOTE] Where the key is stored
> Your API key is encrypted at rest on the device before it is written to storage. It is sent to your server on each request, and nowhere else.

Once the connection is saved, go back to the chat screen and send your first message. See [Chat and Sessions](chat.md) for what you can do from there, or [Voice](voice.md) to set up hands-free use.
