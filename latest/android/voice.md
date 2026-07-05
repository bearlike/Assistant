# Voice

Aura is built to be used by voice as well as by touch. You can dictate a message instead of typing it, have replies read back to you, and raise the assistant hands-free from anywhere on the phone. Everything you do by voice runs through the same Mewbo server and the same session as everything you do by touch.

> [!NOTE] Voice needs on-device speech
> Dictation uses your phone's speech recognizer. Read-aloud uses your phone's text-to-speech engine. Both ship on standard Android devices. Emulators without these components fall back to test stand-ins, so use a real phone to try voice.

## Dictate a message {#dictation}

The chat composer has a microphone button labeled "Dictate." Tap it to speak your message instead of typing it.

The first time, Aura asks for microphone permission. Grant it, and dictation starts. The center of the composer turns into a live view: animated bars show that it is listening, and as you speak your words appear and settle into text.

When you finish, the recognized text drops into the input field with the cursor at the end. You can edit it before sending, or send it straight away. A **Stop** control ends listening without sending, and keeps whatever was recognized so far. If the recognizer hears nothing, it quietly returns to the resting state with no error to dismiss.

Dictation in chat only starts when you tap the microphone. It never listens on its own.

> [!TIP] Dictating turns on read-aloud for that reply
> When you send a message you dictated, Aura reads the reply back to you as it streams. When you type a message, the reply stays silent. Choosing voice or text is how you choose whether to hear the answer.

## Have replies read aloud {#read-aloud}

When you start a turn by voice, Aura speaks the reply as it arrives. It reads sentence by sentence while the text is still streaming, so you hear the answer without waiting for it to finish. Formatting is smoothed for speech: it reads the words, not the markup, and it skips over code blocks and tool activity rather than reading them out.

You can also hear any reply on demand. Every completed answer has a read-aloud button (a speaker icon). Tap it to hear that message. Tap it again, or use the stop-speaking control, to stop.

Speech gets out of your way when you move on. Starting a new voice turn, tapping stop, or dismissing the conversation stops the current reading immediately. While Aura speaks, it briefly lowers other audio playing on the phone, then restores it when it is done.

## Hands-free voice overlay {#overlay}

Aura can act as your phone's assistant. Once you set it as the device's default assistant app, you can raise it from any screen with your usual assistant gesture, without opening the app first.

<video controls preload="metadata" style="max-width: 100%; max-height: min(70vh, 640px); width: auto; height: auto; display: block; margin: 1.5rem auto;">
  <source src="../../assets/videos/Mewbo-Aura-2-Overlay.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

Set this up in Aura's Settings with the option to make Aura your default assistant. That opens the Android setting where you choose which app answers the assistant gesture.

### What happens when you raise it {#overlay-flow}

When you invoke the assistant, a compact overlay slides up over whatever you are doing. A soft blue glow rises from the bottom edge, and a floating composer appears above it. Aura starts listening right away, so you can just speak. There is nothing to tap first (as long as you have granted microphone access).

When you stop speaking, Aura sends the turn automatically. You do not press send. The answer streams into a card in the overlay and is read aloud, so you get a spoken reply in place, without leaving the screen you were on. If you stay silent for a few seconds, listening cancels and the overlay returns to rest.

The response card gives you a few controls: toggle the spoken reply on or off, expand the card, or stop the streaming reply. Swiping the card up, or expanding it, opens the full conversation in the app.

The overlay handles your first exchange in place. From your next message onward, Aura hands the conversation off to the full app and continues it there, so a longer back-and-forth gets the room it needs. After the first reply, a plain microphone button lets you start another spoken turn.

If you just want to pick up where you left off, the overlay offers a shortcut to continue your most recent conversation. Swiping the composer up at any point hands off to the app, carrying anything you have typed. Pressing back or swiping down dismisses the overlay.

### The brand mark {#brand-mark}

Aura's visual signature is an animated mark shaped like an eight-petaled flower. You see it as the app icon and as a small "spark" that pulses at the bottom of the transcript while the assistant is working. In the chat and overlay, the sign that the assistant is live is a gentle blue glow along the bottom edge of the screen. At rest, the background is solid and calm.

## Next steps

- [Chat and Sessions](chat.md): the transcript, session rail, and attachments a voice turn lands in.
- [Device Tools](device-tools.md): what the assistant can do on the phone once you can talk to it.
