# Remote Sync

The terminal client is local-first. The engine runs on your machine. Your session transcript is stored on your machine as a local JSONL file, and that file is the source of truth. Nothing leaves your machine unless you opt in.

Remote sync is the opt-in. When you turn it on, the client keeps running the engine locally, and it also mirrors your session to a remote Mewbo deployment. This gives you cross-device visibility into your terminal sessions. The seam lives in [`cli_remote.py`](repo:apps/mewbo_cli/src/mewbo_cli/cli_remote.py).

## The config block

Remote sync is controlled by one CLI-only config block, `cli.remote`. It has two keys. The model is defined in [`config.py`](repo:packages/mewbo_core/src/mewbo_core/config.py).

| Key | Purpose |
|-----|---------|
| `cli.remote.base_url` | The root URL of the remote Mewbo deployment. It serves the REST API under `/api` and the Mewbo MCP server under `/mcp`. Empty by default. |
| `cli.remote.token` | The API token presented to that deployment. It is write-only, so the console never returns its value. |

The block is CLI-only. Every other Mewbo surface ignores it. Add it to your `configs/app.json` under a `cli` section.

```json
{
  "cli": {
    "remote": {
      "base_url": "https://mewbo.example.com",
      "token": "${MEWBO_REMOTE_TOKEN}"
    }
  }
}
```

The token field expands environment references at use time. A value like `${MEWBO_REMOTE_TOKEN}` reads from your environment, so you keep the secret out of the file.

## What leaves your machine when enabled

!!! warning "Enabling remote sync sends your data to the configured server"
    When `cli.remote.base_url` is set, the client sends your session transcript and event data to that server. Turn it on only for a deployment you trust.

Two things reach the network once a base URL is set.

- The client mirrors each session event to the remote REST API. It POSTs the events fire-and-forget to [POST /api/sessions/{session_id}/events](endpoint:POST /api/sessions/{session_id}/events). It batches a burst of streaming deltas into one request, and it mirrors only the session you are actively working on. A failed POST is logged and dropped. Your local JSONL file stays authoritative either way.
- The client registers the Mewbo MCP server from the same deployment. This surfaces the Mewbo product tools, such as the wiki and search tools, in your terminal session. Those tools then execute remotely on the deployment.

The engine never runs remotely. Only the transcript mirror and the product tools reach the network. The run loop and the authoritative transcript stay on your host.

## The visible indicator

The header tells you which mode you are in. A local session shows `local-only`. A synced session shows `remote:` followed by the base URL. The synced session also carries a `transcript_sink` chip on its trace, so you can filter synced terminal work apart from local work. A local session carries no such chip, because the absence means local.

## How to turn it off

Leave `cli.remote.base_url` empty, or remove the `cli.remote` block. Empty means fully local, and that is the default. With no base URL set, the client never mirrors and never registers the remote tools.

## Next steps

- [Configuration](configuration.md): flags, the config chain, and session recovery.
- [The Interface](interface.md): the live transcript and approval prompts.
- [REST API](../api/index.md): the remote deployment your terminal sessions mirror to.
