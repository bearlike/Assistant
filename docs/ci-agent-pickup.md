# CI Agent Pickup

Assign a bot account to an issue, or @mention it in a comment, and Mewbo picks the item up: a CI workflow ([`.github/workflows/agent-pickup.yml`](repo:.github/workflows/agent-pickup.yml)) collects the issue/PR details and POSTs them to [`POST /api/automation/vcs-pickup`](endpoint:POST /api/automation/vcs-pickup) on your Mewbo API, which starts (or continues) an agent session in the right working directory. The same workflow file runs on both **GitHub Actions** and **Gitea Actions**.

## How It Works

```
assign bot / @mention bot / workflow_dispatch
        │
        ▼
.github/workflows/agent-pickup.yml          (CI runner, read-only token)
        │  resolves item details + PR branch, builds JSON payload
        ▼
POST /api/automation/vcs-pickup             (Mewbo API, X-API-Key auth)
        │  resolves owner/repo → project, prepares branch/worktree
        ▼
Agent session  tag: vcs:<owner/repo>:<kind>:<number>
        │  on run completion (session-end hook)
        ▼
Final answer posted back to the issue/PR as a comment by the bot account
```

**Triggers.** The workflow fires on `issues: [assigned]`, `pull_request: [assigned]`, `issue_comment: [created]`, and manual `workflow_dispatch` (inputs: `issue_number` required, `prompt` optional). A job-level guard then decides whether to run:

- **Assignment** — runs when the just-assigned user is `AGENT_BOT_LOGIN`, with a fallback to checking the item's full assignees list (needed for Gitea, see below).
- **Comment** — runs when the comment body contains `@<AGENT_BOT_LOGIN>` and the comment author is **not** the bot itself (self-trigger loop guard).
- **Dispatch** — always runs (manual override). The workflow fetches the item's title/body/URL from the VCS API since the dispatch payload only carries a number.

A concurrency group keyed on the item number serializes runs per issue/PR (without cancelling in-flight ones).

**Session continuity.** The endpoint derives a deterministic session tag — `vcs:<owner/repo>:<kind>:<number>` (kind is `issue` or `pull_request`) — so every trigger on the same item lands in **one continuous conversation**. A repeat @mention continues the existing session; if a run is currently active, the new prompt is enqueued as a steering message into the running session instead of starting a second run.

**Working directory.**

- **Pull request pickups** run in a **managed git worktree** checked out on the PR head branch. The endpoint fetches the branch from `origin`, creates a local tracking branch if needed, finds-or-creates the worktree (recreating it if the session-end reaper removed a clean one between mentions), and best-effort fast-forwards it to `origin/<branch>`. The agent is instructed to continue from the branch state, commit, and push so the PR updates.
- **Issue pickups** run in an **isolated worktree cut from HEAD** — a deterministic `mewbo/issue-<number>` branch created from the default branch's latest commit. So the agent works in isolation (concurrent issue pickups never collide), commits to that branch, and opens a pull request referencing the issue. The branch is mewbo-owned, so the session-end reaper deletes it with the worktree; a repeat pickup of the same issue reuses the branch (the deterministic session tag keeps the conversation continuous). If the project has no managed parent or the worktree can't be created (e.g. a non-git project path), the pickup **degrades gracefully** to the shared main checkout and the agent is told to cut its own feature branch.

## Setup — GitHub Actions

The workflow file ships in the repo at [`.github/workflows/agent-pickup.yml`](repo:.github/workflows/agent-pickup.yml); you only need to configure secrets and variables (Settings → Secrets and variables → Actions).

**Repository secrets:**

| Secret | Required | Purpose |
|--------|----------|---------|
| `MEWBO_API_URL` | Yes | Base URL of your Mewbo API (e.g. `https://mewbo.example.com`). |
| `MEWBO_API_TOKEN` | Yes | A **provisioned** Mewbo API key, sent as `X-API-Key`. |

**Provisioning the API key.** Mint a dedicated, revocable key instead of using
the master token — keys minted via the key store authenticate every
API-key-gated route, including [`/api/automation/vcs-pickup`](endpoint:POST /api/automation/vcs-pickup):

- Console: **Settings → Security → API keys** → create a key labeled for the
  repo (e.g. `agent-pickup CI`), or
- API: [`POST /api/keys`](endpoint:POST /api/keys) with the master token:

  ```bash
  curl -X POST "$MEWBO_API_URL/api/keys" \
    -H "X-API-Key: $MASTER_TOKEN" -H "Content-Type: application/json" \
    -d '{"label": "agent-pickup CI (owner/repo)"}'
  # → {"id": ..., "key": "mk_..."}  — the plaintext is shown exactly once
  ```

Store the returned `mk_...` value as the `MEWBO_API_TOKEN` repository secret.
Revoking the key ([`DELETE /api/keys/<id>`](endpoint:DELETE /api/keys/<id>)) immediately disables every workflow
that uses it, without touching the master token.

**Repository variables:**

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGENT_BOT_LOGIN` | Yes | Bot account login to watch for (e.g. `mewbo-ai`). Without it, only `workflow_dispatch` triggers run. |
| `AGENT_PROJECT` | No | Mewbo project key override. Defaults to `owner/repo`. |
| `AGENT_MODEL` | No | LLM model override for the session. |
| `AGENT_MODE` | No | `plan` or `act`. |

**Token scope.** The workflow declares least-privilege permissions — `contents: read`, `issues: read`, `pull-requests: read`. The built-in `GITHUB_TOKEN` is used only to **GET** issue/PR details (for dispatch- and comment-triggered pickups that lack inline payload data). The workflow never writes to the repository; all work happens server-side in the Mewbo session.

> [!IMPORTANT]
> GitHub does not expose repository secrets to workflows triggered from **fork** pull requests. Assignment-triggered pickup therefore only works for PRs from same-repo branches; a fork PR's run will fail the configuration check (no `MEWBO_API_URL`).

## Setup — Gitea Actions

Gitea Actions reads the **same file** — it picks up workflows from [`.github/workflows/agent-pickup.yml`](repo:.github/workflows/agent-pickup.yml), so nothing extra needs committing. Configure the same secrets and variables under **repo Settings → Actions → Secrets** and **→ Variables**.

Differences from GitHub that the workflow handles inline:

- **Assignment payload.** Gitea's `assigned` event has no top-level `event.assignee`, so the guard falls back to checking the item's assignees list. Consequence: a re-assignment event on an item where the bot is *already* assigned (e.g. assigning a second person) can re-trigger the workflow. This is harmless — the endpoint resolves the same session tag and reuses the existing session.
- **API URL.** Gitea's `act_runner` may leave `github.api_url` empty; the workflow derives `<server_url>/api/v1` itself. The `/repos/{owner}/{repo}/issues/{n}` and `/pulls/{n}` shapes it uses are identical on both platforms, and both accept the workflow token via `Authorization: token ...`.
- **Provider field.** The payload's `provider` is set to `gitea` whenever `server_url` is not `https://github.com` (informational only).

Runner requirements: a runner registered with the **`ubuntu-latest`** label must exist. `jq` is auto-installed via `apt-get` if missing from the runner image.

## Server-Side Requirements

The Mewbo API must be able to map the `owner/repo` string to a local project:

- **Automatic** — a configured project whose git remote matches the repository. Resolution uses the same [`RepoIdentity`](repo:apps/mewbo_api/src/mewbo_api/repo_identity.py) alias matching as the worktree routes, so the repo resolves via its Gitea host URL, a GitHub mirror URL, `owner/repo`, or the bare repo name.
- **Explicit** — set the `AGENT_PROJECT` repository variable to a Mewbo project key, which overrides the `owner/repo` default.

The project path must be a **git clone with an `origin` remote** that can fetch PR branches — PR pickups run `git fetch origin <branch>` in the parent clone before creating the worktree, and a pickup whose branch cannot be fetched fails with `422`.

### Replies back to the issue/PR

When a pickup session's run ends, a session-end hook posts the agent's **final answer back to the originating issue/PR as a comment**, authored by the bot account — the same completion-hook mechanism the chat channels (Nextcloud Talk, email) use to deliver their replies. Configure a forge token for the bot under `channels.vcs` in the server config:

```json
"channels": {
  "vcs": {
    "tokens": { "git.example.com": "<bot PAT>", "api.github.com": "<bot PAT>" },
    "tls_verify": true
  }
}
```

- **Tokens are keyed by forge API host** (the hostname of the `api_url` the workflow sends), so one Mewbo instance can reply on several forges.
- **Token identity = comment author.** Mint the PAT *for the bot account* (on Gitea, an admin can: `POST /api/v1/users/<bot>/tokens` with basic auth, scope `write:issue`; on GitHub use a fine-grained PAT with *Issues: write* + *Pull requests: write*). The `/repos/{owner}/{repo}/issues/{n}/comments` endpoint and `Authorization: token` scheme are identical on GitHub and Gitea.
- **Without a token the reply leg is silently disabled** — pickups still run; the answer is only visible in the session.
- `tls_verify: false` opts out of certificate verification for forges behind an internal CA the API host does not trust (the Python client uses the system CA store, same as git).
- Loop safety: the bot's own comment never re-triggers a pickup — the workflow guard requires the comment author ≠ bot, and the endpoint suppresses self-comments too.
- Answers longer than ~60 000 characters are truncated to fit forge comment limits.

## Endpoint Reference

### `POST /api/automation/vcs-pickup`

Auth: `X-API-Key` header (the standard API key). Body is strict JSON (unknown fields are rejected):

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `repository` | string | Yes | `owner/repo` of the triggering repository. |
| `kind` | `issue` \| `pull_request` | Yes | Item kind. |
| `number` | int ≥ 1 | Yes | Issue/PR number. |
| `provider` | string | No | `github` or `gitea` — informational. |
| `api_url` | string | No | Forge REST base URL — enables posting the final answer back as a comment. |
| `event` | string | No | Triggering event, e.g. `issues`, `issue_comment`. |
| `url` | string | No | Item HTML URL. |
| `title` | string | No | Item title. |
| `body` | string | No | Item description (workflow truncates at 20 000 chars). |
| `comment` | string | No | The @mention comment text, when comment-triggered. |
| `comment_author` | string | No | Login of the comment author. |
| `assignee` | string | No | Login of the just-assigned user. |
| `bot_login` | string | No | Configured bot login, for self-trigger suppression. |
| `head_ref` | string | No | PR head branch — presence makes a PR pickup worktree-bound. |
| `base_ref` | string | No | PR base branch. |
| `project` | string | No | Project key override; defaults to `repository`. |
| `model` | string | No | LLM model override. |
| `mode` | `plan` \| `act` | No | Session mode. |
| `prompt` | string | No | Full override of the generated pickup prompt. |

**Self-trigger suppression.** When `bot_login` is set and equals `comment_author`, the endpoint returns `200 {"skipped": true, "reason": "comment author is the bot"}` without starting anything. The workflow guards this too — defense in depth against the bot replying to its own comments in a loop.

**Responses:**

| Status | Body | Meaning |
|--------|------|---------|
| `200` | `{"session_id", "session_tag", "run_id", "resumed", "worktree_id", "accepted": true}` | Run started. `resumed` is `true` when the tag matched an existing session; `worktree_id` is set for PR pickups and for issue pickups that got an isolated worktree, `null` when an issue pickup degraded to the main checkout. |
| `202` | `{"session_id", "session_tag", "enqueued": true, "resumed": true}` | A run was already active for this item — the prompt was enqueued as a steering message. |
| `200` | `{"skipped": true, "reason": ...}` | Self-trigger suppressed. |
| `400` | `{"message": "Invalid input: ..."}` | Body failed validation. |
| `404` | error | `repository`/`project` did not resolve to a configured project. |
| `409` | `{"message": "Session is already running."}` | Concurrent-start race lost (rare; the active-run path normally returns `202`). |
| `422` | `{"message": "Failed to prepare branch/worktree ..."}` | PR branch could not be fetched or the worktree could not be created. |

## Testing

A safe, incremental verification path (issue #72 acceptance criteria):

1. **Manual dispatch first.** Run the *Agent Pickup* workflow via `workflow_dispatch` with a test issue number. This skips the assignment guard entirely and validates secrets, API reachability, and project resolution. The job log prints the JSON response and the session id.
2. **Scratch issue + assignment.** Create a throwaway issue, assign the bot account, and watch the Actions run start and a session appear in the Mewbo console (tagged `vcs:<owner/repo>:issue:<n>`, on an isolated `mewbo/issue-<n>` worktree cut from HEAD).
3. **Negative check.** Assign a non-bot user to the same issue — the guard must skip the job (no run).
4. **PR @mention.** Comment `@<bot-login> <request>` on a pull request — a session should start in a managed worktree on the PR head branch. Mention again to confirm the same session continues.

To test the endpoint directly (bypassing CI):

```bash
curl -X POST "$MEWBO_API_URL/api/automation/vcs-pickup" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEWBO_API_TOKEN" \
  -d '{
    "repository": "owner/repo",
    "kind": "issue",
    "number": 72,
    "provider": "gitea",
    "event": "workflow_dispatch",
    "title": "Scratch issue for agent pickup",
    "body": "Reply with a one-line acknowledgement.",
    "bot_login": "mewbo-ai"
  }'
```

A `200` with a `session_id` confirms the server side end to end; repeat the call to see `"resumed": true` (or a `202` steering response if the first run is still active).
