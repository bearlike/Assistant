# Automation

Assign a bot account to an issue, or mention it in a comment, and Mewbo picks the item up. A CI workflow ([`agent-pickup.yml`](repo:.github/workflows/agent-pickup.yml)) collects the issue or pull request details and POSTs them to [POST /api/automation/vcs-pickup](endpoint:POST /api/automation/vcs-pickup) on your Mewbo API. The API starts, or continues, an agent session in the right working directory. The same workflow file runs on both GitHub Actions and Gitea Actions.

## How it works

```
assign bot / mention bot / workflow_dispatch
        │
        ▼
.github/workflows/agent-pickup.yml          (CI runner, read-only token)
        │  resolves item details + PR branch, builds JSON payload
        ▼
POST /api/automation/vcs-pickup             (Mewbo API, X-API-Key auth)
        │  resolves owner/repo -> project, prepares branch/worktree
        ▼
Agent session  tag: vcs:<owner/repo>:<kind>:<number>
        │  on run completion (session-end hook)
        ▼
Final answer posted back to the issue/PR as a comment by the bot account
```

**Triggers.** The workflow fires on `issues: [assigned]`, `pull_request: [assigned]`, `issue_comment: [created]`, and manual `workflow_dispatch` (inputs: `issue_number` required, `prompt` optional). A job-level guard then decides whether to run:

- **Assignment.** Runs when the just-assigned user is `AGENT_BOT_LOGIN`, with a fallback to checking the item's full assignees list (needed for Gitea, see below).
- **Comment.** Runs when the comment body contains `@<AGENT_BOT_LOGIN>` and the comment author is not the bot itself (a self-trigger loop guard).
- **Dispatch.** Always runs (manual override). The workflow fetches the item's title, body, and URL from the VCS API, since the dispatch payload only carries a number.

A concurrency group keyed on the item number serializes runs per issue or PR, without cancelling in-flight ones.

**Session continuity.** The endpoint derives a deterministic session tag, `vcs:<owner/repo>:<kind>:<number>` (kind is `issue` or `pull_request`), so every trigger on the same item lands in one continuous conversation. A repeat mention continues the existing session. If a run is currently active, the new prompt is enqueued as a steering message into the running session instead of starting a second run.

**Working directory.**

- **Pull request pickups** run in a managed git worktree checked out on the PR head branch. The endpoint fetches the branch from `origin`, creates a local tracking branch if needed, finds or creates the worktree (recreating it if the session-end reaper removed a clean one between mentions), and best-effort fast-forwards it to `origin/<branch>`. The agent is instructed to continue from the branch state, commit, and push so the PR updates.
- **Issue pickups** run in an isolated worktree cut from HEAD. This is a deterministic `mewbo/issue-<number>` branch created from the default branch's latest commit. The agent works in isolation, so concurrent issue pickups never collide. It commits to that branch and opens a pull request referencing the issue. The branch is mewbo-owned, so the session-end reaper deletes it with the worktree. A repeat pickup of the same issue reuses the branch, and the deterministic session tag keeps the conversation continuous. If the project has no managed parent, or the worktree cannot be created (for example a non-git project path), the pickup degrades gracefully to the shared main checkout, and the agent is told to cut its own feature branch.

## The pickup prompt closes the issue

For an issue pickup, the generated prompt instructs the agent to include a `Closes #<issue-number>` directive in its pull request description. Merging that pull request then closes the issue automatically, through the forge's closing-keyword convention. The issue number is the one the workflow posted, so a pickup of issue 42 renders the literal instruction `Closes #42`.

This applies to issue pickups only. A pull request pickup already runs on the PR's own branch, so its prompt tells the agent to update the existing PR rather than open a new one. Supplying a full `prompt` override in the request body replaces the generated prompt entirely, in which case the `Closes #` directive is not added.

## Setup, GitHub Actions

The workflow file ships in the repo at [`agent-pickup.yml`](repo:.github/workflows/agent-pickup.yml). You only need to configure secrets and variables (Settings, then Secrets and variables, then Actions).

**Repository secrets:**

| Secret | Required | Purpose |
|--------|----------|---------|
| `MEWBO_API_URL` | Yes | Base URL of your Mewbo API (for example `https://mewbo.example.com`). |
| `MEWBO_API_TOKEN` | Yes | A provisioned Mewbo API key, sent as `X-API-Key`. |

**Provisioning the API key.** Mint a dedicated, revocable key instead of using the master token. Keys minted through the key store authenticate every API-key-gated route, including [POST /api/automation/vcs-pickup](endpoint:POST /api/automation/vcs-pickup):

- Console: Settings, then API Keys, then create a key labeled for the repo (for example `agent-pickup CI`), or
- API: [POST /api/keys](endpoint:POST /api/keys) with the master token:

  ```bash
  curl -X POST "$MEWBO_API_URL/api/keys" \
    -H "X-API-Key: $MASTER_TOKEN" -H "Content-Type: application/json" \
    -d '{"label": "agent-pickup CI (owner/repo)"}'
  # -> {"id": ..., "key": "mk_..."}  the plaintext is shown exactly once
  ```

Store the returned `mk_...` value as the `MEWBO_API_TOKEN` repository secret. Revoking the key ([DELETE /api/keys/{key_id}](endpoint:DELETE /api/keys/{key_id})) immediately disables every workflow that uses it, without touching the master token.

**Repository variables:**

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGENT_BOT_LOGIN` | Yes | Bot account login to watch for (for example `mewbo-ai`). Without it, only `workflow_dispatch` triggers run. |
| `AGENT_PROJECT` | No | Mewbo project key override. Defaults to `owner/repo`. |
| `AGENT_MODEL` | No | LLM model override for the session. |
| `AGENT_MODE` | No | `plan` or `act`. |

**Token scope.** The workflow declares least-privilege permissions: `contents: read`, `issues: read`, `pull-requests: read`. The built-in `GITHUB_TOKEN` is used only to GET issue or PR details, for dispatch-triggered and comment-triggered pickups that lack inline payload data. The workflow never writes to the repository. All work happens server-side in the Mewbo session.

> [!IMPORTANT]
> GitHub does not expose repository secrets to workflows triggered from fork pull requests. Assignment-triggered pickup therefore only works for PRs from same-repo branches. A fork PR's run will fail the configuration check (no `MEWBO_API_URL`).

## Setup, Gitea Actions

Gitea Actions reads the same file. It picks up workflows from [`agent-pickup.yml`](repo:.github/workflows/agent-pickup.yml), so nothing extra needs committing. Configure the same secrets and variables under repo Settings, then Actions, then Secrets, and then Variables.

Differences from GitHub that the workflow handles inline:

- **Assignment payload.** Gitea's `assigned` event has no top-level `event.assignee`, so the guard falls back to checking the item's assignees list. As a consequence, a re-assignment event on an item where the bot is already assigned (for example assigning a second person) can re-trigger the workflow. This is harmless. The endpoint resolves the same session tag and reuses the existing session.
- **API URL.** Gitea's `act_runner` may leave `github.api_url` empty. The workflow derives `<server_url>/api/v1` itself. The `/repos/{owner}/{repo}/issues/{n}` and `/pulls/{n}` shapes it uses are identical on both platforms, and both accept the workflow token via `Authorization: token ...`.
- **Provider field.** The payload's `provider` is set to `gitea` whenever `server_url` is not `https://github.com`. This is informational only.

Runner requirements: a runner registered with the `ubuntu-latest` label must exist. `jq` is auto-installed via `apt-get` if it is missing from the runner image.

## Server-side requirements

The Mewbo API must be able to map the `owner/repo` string to a local project:

- **Automatic.** A configured project whose git remote matches the repository. Resolution uses the same [`RepoIdentity`](repo:apps/mewbo_api/src/mewbo_api/repo_identity.py) alias matching as the worktree routes, so the repo resolves via its Gitea host URL, a GitHub mirror URL, `owner/repo`, or the bare repo name.
- **Explicit.** Set the `AGENT_PROJECT` repository variable to a Mewbo project key, which overrides the `owner/repo` default.

The project path must be a git clone with an `origin` remote that can fetch PR branches. PR pickups run `git fetch origin <branch>` in the parent clone before creating the worktree, and a pickup whose branch cannot be fetched fails with `422`.

### Replies back to the issue or PR

When a pickup session's run ends, a session-end hook posts the agent's final answer back to the originating issue or PR as a comment, authored by the bot account. This is the same completion-hook mechanism the chat channels use to deliver their replies. Configure a forge token for the bot under `channels.vcs` in the server config:

```json
"channels": {
  "vcs": {
    "tokens": { "git.example.com": "<bot PAT>", "api.github.com": "<bot PAT>" },
    "tls_verify": true
  }
}
```

- **Tokens are keyed by forge API host** (the hostname of the `api_url` the workflow sends), so one Mewbo instance can reply on several forges.
- **Token identity equals comment author.** Mint the PAT for the bot account. On Gitea, an admin can `POST /api/v1/users/<bot>/tokens` with basic auth, scopes `read:user`, `write:issue`, `write:repository`. On GitHub, use a fine-grained PAT with Issues write plus Pull requests write. The `/repos/{owner}/{repo}/issues/{n}/comments` endpoint and `Authorization: token` scheme are identical on GitHub and Gitea.
- **The same tokens also log in the `tea` CLI.** The api image ships `tea` (Gitea) and `gh` (GitHub), and [`15-tea-setup.sh`](repo:docker/init.d/15-tea-setup.sh) adds a `tea` login per `channels.vcs.tokens` host at container start (falling back to `~/.git-credentials` for hosts without one). So when the agent opens a pull request, it can do it as the bot account. The `read:user` scope is what `tea login add` needs, and `write:repository` covers PR creation.
- **Without a token the reply leg is silently disabled.** Pickups still run. The answer is only visible in the session.
- `tls_verify: false` opts out of certificate verification for forges behind an internal CA the API host does not trust (the Python client uses the system CA store, same as git).
- Loop safety: the bot's own comment never re-triggers a pickup. The workflow guard requires the comment author to differ from the bot, and the endpoint suppresses self-comments too.
- Answers longer than roughly 60,000 characters are truncated to fit forge comment limits.

## Endpoint reference

### POST /api/automation/vcs-pickup

Auth: `X-API-Key` header (the standard API key). The body is strict JSON, and unknown fields are rejected:

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `repository` | string | Yes | `owner/repo` of the triggering repository. |
| `kind` | `issue` or `pull_request` | Yes | Item kind. |
| `number` | int >= 1 | Yes | Issue or PR number. |
| `provider` | string | No | `github` or `gitea`. Informational. |
| `api_url` | string | No | Forge REST base URL. Enables posting the final answer back as a comment. |
| `event` | string | No | Triggering event, for example `issues` or `issue_comment`. |
| `url` | string | No | Item HTML URL. |
| `title` | string | No | Item title. |
| `body` | string | No | Item description (the workflow truncates at 20,000 chars). |
| `comment` | string | No | The mention comment text, when comment-triggered. |
| `comment_author` | string | No | Login of the comment author. |
| `assignee` | string | No | Login of the just-assigned user. |
| `bot_login` | string | No | Configured bot login, for self-trigger suppression. |
| `head_ref` | string | No | PR head branch. Its presence makes a PR pickup worktree-bound. |
| `base_ref` | string | No | PR base branch. |
| `project` | string | No | Project key override. Defaults to `repository`. |
| `model` | string | No | LLM model override. |
| `mode` | `plan` or `act` | No | Session mode. |
| `prompt` | string | No | Full override of the generated pickup prompt. |

**Self-trigger suppression.** When `bot_login` is set and equals `comment_author`, the endpoint returns `200 {"skipped": true, "reason": "comment author is the bot"}` without starting anything. The workflow guards this too, as defense in depth against the bot replying to its own comment in a loop.

**Responses:**

| Status | Body | Meaning |
|--------|------|---------|
| `200` | `{"session_id", "session_tag", "run_id", "resumed", "worktree_id", "accepted": true}` | Run started. `resumed` is `true` when the tag matched an existing session. `worktree_id` is set for PR pickups and for issue pickups that got an isolated worktree, and `null` when an issue pickup degraded to the main checkout. |
| `202` | `{"session_id", "session_tag", "enqueued": true, "resumed": true}` | A run was already active for this item. The prompt was enqueued as a steering message. |
| `200` | `{"skipped": true, "reason": ...}` | Self-trigger suppressed. |
| `400` | `{"message": "Invalid input: ..."}` | Body failed validation. |
| `404` | error | `repository` or `project` did not resolve to a configured project. |
| `409` | `{"message": "Session is already running."}` | Concurrent-start race lost (rare; the active-run path normally returns `202`). |
| `422` | `{"message": "Failed to prepare branch/worktree ..."}` | PR branch could not be fetched, or the worktree could not be created. |

## Testing

A safe, incremental verification path:

1. **Manual dispatch first.** Run the Agent Pickup workflow via `workflow_dispatch` with a test issue number. This skips the assignment guard entirely and validates secrets, API reachability, and project resolution. The job log prints the JSON response and the session id.
2. **Scratch issue and assignment.** Create a throwaway issue, assign the bot account, and watch the Actions run start and a session appear in the Mewbo console (tagged `vcs:<owner/repo>:issue:<n>`, on an isolated `mewbo/issue-<n>` worktree cut from HEAD).
3. **Negative check.** Assign a non-bot user to the same issue. The guard must skip the job, so no run starts.
4. **PR mention.** Comment `@<bot-login> <request>` on a pull request. A session should start in a managed worktree on the PR head branch. Mention again to confirm the same session continues.

To test the endpoint directly, bypassing CI:

```bash
curl -X POST "$MEWBO_API_URL/api/automation/vcs-pickup" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEWBO_API_TOKEN" \
  -d '{
    "repository": "owner/repo",
    "kind": "issue",
    "number": 42,
    "provider": "gitea",
    "event": "workflow_dispatch",
    "title": "Scratch issue for agent pickup",
    "body": "Reply with a one-line acknowledgement.",
    "bot_login": "mewbo-ai"
  }'
```

A `200` with a `session_id` confirms the server side end to end. Repeat the call to see `"resumed": true`, or a `202` steering response if the first run is still active.

## Next steps

- [Building a Client](building-a-client.md): the session and event primitives automation runs ride on.
- [Full API reference](../rest-api.md): every route, parameter, and response shape.
