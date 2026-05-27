"""Static catalogues for the wiki section.

These power /v1/wiki/platforms and /v1/wiki/languages. Model catalog is
NOT defined here — the wiki picker uses the shared /api/models endpoint
(the same source the main composer reads) to stay aligned with whatever
the LiteLLM proxy actually serves. DRY: one model list, one source.
"""
from __future__ import annotations

from mewbo_graph.wiki.types import Language, Platform

# ── Platforms ─────────────────────────────────────────────────────────────────

PLATFORMS: list[Platform] = [
    Platform(
        id="github",
        name="GitHub",
        mono="Gh",
        color="#181717",
        short="github.com or GitHub Enterprise",
        hosts=["github.com"],
        tokenLabel="GitHub Personal Access Token",
        tokenScope="repo (full) for private repos; public_repo is enough for public.",
        tokenUrl="https://github.com/settings/tokens/new",
        tokenSteps=[
            "Open Settings → Developer settings → Personal access tokens",
            "Click Generate new token (classic)",
            "Tick the repo scope, set an expiry, generate",
        ],
    ),
    Platform(
        id="gitlab",
        name="GitLab",
        mono="Gl",
        color="#FC6D26",
        short="gitlab.com or self-hosted GitLab",
        hosts=["gitlab.com"],
        tokenLabel="GitLab Personal Access Token",
        tokenScope="read_repository (read_api too if you want issue/PR context).",
        tokenUrl="https://gitlab.com/-/user_settings/personal_access_tokens",
        tokenSteps=[
            "Open User Settings → Access Tokens",
            "Name the token, set an expiry",
            "Select read_repository, click Create",
        ],
    ),
    Platform(
        id="bitbucket",
        name="Bitbucket",
        mono="Bb",
        color="#0052CC",
        short="bitbucket.org cloud workspaces",
        hosts=["bitbucket.org"],
        tokenLabel="Bitbucket App Password",
        tokenScope="Repository: Read. Use your username with the app password.",
        tokenUrl="https://bitbucket.org/account/settings/app-passwords/",
        tokenSteps=[
            "Open Personal settings → App passwords",
            "Create app password, label it MewboWiki",
            "Grant Repository: Read, then save the secret",
        ],
    ),
    Platform(
        id="gitea",
        name="Gitea",
        mono="Gt",
        color="#609926",
        short="gitea.com or any self-hosted Gitea/Forgejo",
        hosts=["gitea.com", "codeberg.org"],
        tokenLabel="Gitea Access Token",
        tokenScope="Needs read:repository scope.",
        tokenUrl=None,
        tokenSteps=[
            "Open Settings → Applications → Manage Access Tokens",
            "Generate new token",
            "Select read:repository, save the secret",
        ],
    ),
    Platform(
        id="azure",
        name="Azure DevOps",
        mono="Az",
        color="#0078D7",
        short="dev.azure.com repos",
        hosts=["dev.azure.com", "visualstudio.com"],
        tokenLabel="Azure DevOps PAT",
        tokenScope="Code: Read.",
        tokenUrl="https://dev.azure.com/_usersSettings/tokens",
        tokenSteps=[
            "User settings → Personal access tokens",
            "New Token → custom defined",
            "Select Code: Read, save the secret",
        ],
    ),
    Platform(
        id="git",
        name="Generic Git",
        mono="Git",
        color="#F05032",
        short="Any Git URL (SSH or HTTPS)",
        hosts=[],
        tokenLabel="HTTPS credentials (optional)",
        tokenScope="Use a deploy token, app password, or omit for public repos.",
        tokenUrl=None,
        tokenSteps=[
            "If your host issues deploy tokens, paste one here",
            "Otherwise enter credentials in the form user:token",
            "Leave blank for public repositories",
        ],
    ),
]

# ── Languages ─────────────────────────────────────────────────────────────────

LANGUAGES: list[Language] = [
    Language(id="en", label="English", subtle="Default"),
    Language(id="es", label="Español"),
    Language(id="fr", label="Français"),
    Language(id="de", label="Deutsch"),
    Language(id="pt", label="Português"),
    Language(id="it", label="Italiano"),
    Language(id="ja", label="日本語"),
    Language(id="ko", label="한국어"),
    Language(id="zh", label="中文 (简体)"),
    Language(id="zh-tw", label="中文 (繁體)"),
    Language(id="ru", label="Русский"),
    Language(id="hi", label="हिन्दी"),
    Language(id="ar", label="العربية"),
]
