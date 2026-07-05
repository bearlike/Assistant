# Get Started

<video autoplay muted loop playsinline preload="auto" style="width: 100%; max-width: 960px; height: auto; display: block; margin: 0 auto 1.5rem;">
  <source src="../assets/videos/Mewbo-Aura-0-Banner.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

Aura puts your self-hosted Mewbo assistant in your pocket: chat, voice, and real actions on your phone, all wired to a server you run.

The terminal and web console are built for the desk. Aura is for the moments away from it. Dictate a question with your hands full and hear the reply read back. Let the assistant set an alarm, check the battery, or send a text on the very device it runs on. Start a session on the move and pick it up later from any client, because the session lives on your server, not the app.

With Aura you can:

- **Chat with your assistant.** Ask a question and watch the answer stream in as formatted text. Tool calls, plans, and sub-agent work show up as you go, so you can see what the assistant is doing.
- **Talk instead of type.** Dictate a message with your voice. Have replies read back to you as they stream. Raise the assistant hands-free from anywhere on the device.
- **Let the assistant act on your phone.** With your permission, the assistant can check the time and battery, set alarms and timers, and read or send text messages.
- **Keep your work.** Every conversation is a session. Reopen a recent one, rename it, or archive it when you are done.

## Prerequisites {#prerequisites}

Aura is a client, not a standalone app. You need two things before it is useful:

| Requirement | Notes |
|-------------|-------|
| A running Mewbo server | Aura connects to a Mewbo API server that you host. See [Get Started](../getting-started.md) to stand one up. |
| An API key | Aura authenticates to your server with an API key. Issue one from the console (Settings, then API Keys) as described in [MCP Server](../clients-mcp.md#authentication-required). |
| An Android phone | A recent Android device. Voice features need a microphone, and read-aloud needs a text-to-speech engine (both are standard on shipping phones). |

> [!NOTE] Your server, your data
> Aura only ever talks to the server you point it at. There is no Aura cloud service in between. The conversations, the model, and the tools are all yours.

## Your first query {#first-query}

Three steps take you from a fresh install to a working assistant.

1. **Install the app.** Download a prebuilt APK or build it from source. See [Install](install.md).
2. **Connect it to your server.** On first launch, open Settings and enter your server's base URL and your API key. Aura checks the connection before it saves. See [Install](install.md#connect) for the details.
3. **Ask something.** Type a message in the composer at the bottom and send it. The reply streams in as formatted text. That is a session, and it is now saved to your recent chats.

From there you can pick a different model, scope a session to a project, attach a file, or switch to voice.

## Next steps { .ms-h2-icon data-icon="rocket" }

<div class="ms-grid ms-grid--4">

<a class="ms-card" href="install/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:download" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Install</span>
  <span class="ms-card__body">Get the APK or build from source, choose a build flavor, and connect to your server.</span>
</a>

<a class="ms-card" href="voice/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:mic" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Voice</span>
  <span class="ms-card__body">Dictation, read-aloud replies, and the hands-free voice overlay.</span>
</a>

<a class="ms-card" href="chat/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:messages-square" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Chat and Sessions</span>
  <span class="ms-card__body">Streaming chat, tool activity, the session rail, attachments, and the model and scope pickers.</span>
</a>

<a class="ms-card" href="device-tools/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:smartphone-nfc" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Device Tools</span>
  <span class="ms-card__body">What the assistant can do on your phone, and how permissions gate it.</span>
</a>

</div>
