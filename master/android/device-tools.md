# Device Tools

Aura lets the assistant act on your phone. During a conversation, the assistant can check the time and battery, set alarms and timers, get your attention, and read or send text messages. These are real actions on your device, run by Aura on the assistant's behalf.

## How a device tool call works {#round-trip}

Device tools round-trip between your server and your phone. Nothing runs on the phone unless the assistant asks for it during a live conversation.

1. **Aura advertises what it can do.** When a session starts, Aura tells the server which device tools it can offer. That list depends on what your phone currently allows.
2. **The assistant asks.** If the assistant decides to use one, the server sends a tool call down the session's live stream, [GET /api/sessions/{session_id}/stream](endpoint:GET /api/sessions/{session_id}/stream).
3. **Aura runs it on the phone.** Aura executes the matching action locally.
4. **Aura reports back.** Aura posts the result to [POST /api/sessions/{session_id}/device_tools/{call_id}/result](endpoint:POST /api/sessions/{session_id}/device_tools/{call_id}/result), and the assistant continues its answer using what came back.

If a call arrives after it has expired, or an action fails, Aura reports that back too, so the assistant is never left waiting on a silent tool.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-aura-04-device-tools.png" alt="Aura chat turn asking for the phone's battery level, with the tool-activity fold expanded to show the device_get_battery call and a checkmark, followed by the assistant's answer using that result" style="width: 100%; max-width: 360px; height: auto;" />
</div>

## Permissions are the only gate {#permissions}

Android's own runtime permissions are the single control over what the assistant can do on your phone. This is the important thing to understand about device tools.

- There is no separate in-app consent screen, allowlist, or extra confirmation layer. Aura does not add its own approval step on top of Android.
- A tool that needs a permission you have not granted is not even offered to the server. The assistant cannot call a tool it was never told about.
- Granting the Android permission is what enables the tool. Revoking it takes the tool away again.

The tools that touch messaging need permission: reading and sending text messages require the Android SMS permissions. Grant SMS access from Aura's Settings, or accept the system prompt when it appears. Time, battery, alarms, timers, and the attention tool need no special permission and are available by default.

> [!NOTE] You are in control through Android
> Because the phone's permission system is the only gate, you manage what the assistant can do the same way you manage any app: in Android's own permission settings. Turn a permission off and the matching tool disappears from what the assistant can reach.

## Available tools {#tools}

Aura ships the following device tools. The exact set advertised in any session is filtered down to the ones your phone currently permits. The catalog of every tool this build carries lives in [`DeviceToolCatalog.kt`](repo:apps/mewbo_aura/app/src/main/java/com/mewbo/aura/data/device/DeviceToolCatalog.kt).

| Tool | What it does | Permission |
|------|--------------|------------|
| Get time | Reports the phone's current local date, time, and timezone. | None |
| Get battery | Reports the battery level and whether the phone is charging. | None |
| Set alarm | Asks the phone's clock app to set an alarm for a given time. | None |
| Set timer | Asks the phone's clock app to start a countdown timer. | None |
| Get next alarm | Reports the single next alarm scheduled to fire. Android only exposes the soonest one, not every alarm. | None |
| Dismiss alarm | Asks the clock app to dismiss an alarm by time, by label, the next one, or all of them. | None |
| Wake | Rings and vibrates the phone briefly to get your attention. | None |
| Read latest texts | Reads your most recent incoming text messages, optionally filtered by sender. | Read SMS |
| Send a text | Sends a text message to a recipient. | Send SMS |

> [!WARNING] Sending a text is irreversible
> The send-a-text tool sends a real SMS, which cannot be recalled and may cost money through your carrier. It is gated behind the Android Send SMS permission, which you grant explicitly. If you never grant that permission, the assistant is never offered the tool.

## Next steps

- [Chat and Sessions](chat.md): where tool activity appears during a conversation.
- [Device Tool Bridge](../api/device-tools.md): how the server delivers these calls to the app.
