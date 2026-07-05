# Chat and Sessions

Chat is the heart of Aura. You type in the composer at the bottom, and the reply streams in above it. Along the way you can see what the assistant is doing, attach files, pick a model, and scope the work to a project. Every conversation is saved as a session you can return to.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-aura-01-chat.png" alt="Aura chat screen showing a user's request for a git command cheat sheet and the assistant's completed reply rendered as markdown, with a heading, bullet list, fenced code block, and the action row underneath" style="width: 100%; max-width: 360px; height: auto;" />
</div>

## Streaming chat {#streaming}

Send a message and the reply arrives as it is generated. Aura renders it as formatted markdown live, so headings, lists, code, and emphasis appear as they stream, not as raw text that reformats at the end. Your own messages sit in bubbles on the right. The assistant's replies run full width below them.

<video controls preload="metadata" style="max-width: 100%; max-height: min(70vh, 640px); width: auto; height: auto; display: block; margin: 1.5rem auto;">
  <source src="../../assets/videos/Mewbo-Aura-1-Chat.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

Under each finished reply is a row of actions: a thumbs up and down, a copy button, an option to select text, and the read-aloud speaker button covered in [Voice](voice.md). A single reminder that the assistant can make mistakes sits at the very bottom of the transcript, once per conversation.

## Tool and agent activity {#activity}

The assistant often does work before it answers: it calls tools, follows a plan, or delegates to a sub-agent. Aura shows that activity in the transcript, always above the reply it belongs to, so you can read the answer last.

- **Tool calls** fold into a single row per turn. It reads "Using N tools" while the work runs, then "Used N tools" when it settles. Tap it to expand the individual calls, and tap a call to see its input and result.
- **Plans** show as a checklist that fills in as steps complete.
- **Sub-agents** show as a small row naming the sub-agent and its status.

Everything stays collapsed by default, so a busy turn stays readable. You open only the parts you want to inspect.

## The session rail {#rail}

Open the navigation drawer to see your recent conversations. It slides in from the side and lists your sessions, grouped into Today, Previous 7 days, and Older. A running conversation shows a small dot. Tap any row to reopen that session and pick up where it left off.

<div style="display: flex; justify-content: center;">
  <img src="../../assets/img/mewbo-aura-02-sessions.png" alt="Aura's navigation drawer open, showing New chat and Search chats shortcuts above a Recents list of several sessions grouped under Today" style="width: 100%; max-width: 360px; height: auto;" />
</div>

At the top of the drawer are shortcuts to start a **New chat** or **Search chats**. The Recents header has a filter that switches between chats you started on this phone (the default) and all of your sessions from every client.

### Rename or archive a session {#rename-archive}

Long-press a session in the rail to manage it. A sheet slides up with two actions:

- **Rename** opens a field with the current title selected, so you can type a new one and confirm.
- **Archive** removes the session from your recent list. Archived sessions are hidden from the rail but not destroyed.

> [!NOTE] There is no delete
> Aura does not delete sessions. Archive hides a session from your recents, and that is the strongest action offered. Your history stays on the server.

## Attachments {#attachments}

You can send files and images with a message. Tap the plus button in the composer to open the attachment options, then choose **Photos** to pick an image or **Files** to pick a document (PDF, common office formats, and text formats like CSV, Markdown, and JSON).

Before you send, each attachment appears as a chip in the composer with its name and a way to remove it. After you send, the attachments appear as cards above your message in the transcript.

Images need a model that can see them. If the model you have selected does not support vision, the Photos option is disabled and tells you to pick a vision model. Documents work with any model. The assistant reads a document's contents as part of your message, and sees an image directly when the model supports it.

## Pick a model {#model-picker}

The title bar at the top shows the current model with a chevron. Tap it to open the model picker. Popular models are listed first with friendly names over their full identifiers, and a "More models" row reveals the rest. A checkmark shows your current choice. The list comes from your server, so you see exactly the models your deployment offers. You can switch model at any time, even mid-conversation.

## Scope a session to a project and tools {#scope}

By default a new chat runs in a temporary workspace with every tool available. You can change both from the composer's plus menu, in its Session section.

- **Project** lets you attach the conversation to one of your registered projects, so the assistant works in that project's directory instead of a temporary one. The default is "Temporary," meaning no project.
- **Tools** lets you choose which tools and integrations the assistant may use. Tool groups (such as connected servers and product features) each have a switch, and you can expand a group to toggle individual tools. Leaving everything on gives the assistant its full toolset. Narrowing the selection restricts it to what you allow.

Before a conversation has any messages, a small indicator above the composer summarizes the current scope, for example the project name and the number of tools enabled. Tap it to open the same options. You set the scope for a chat when you start it. Once a turn is in flight the scope is fixed for that chat, and shown but not editable. Your default project comes from Settings, and a per-chat choice overrides it until your next new chat.

## Next steps

- [Voice](voice.md): dictate turns, hear replies read aloud, and go hands-free with the overlay.
- [Device Tools](device-tools.md): let the assistant act on the phone itself.
