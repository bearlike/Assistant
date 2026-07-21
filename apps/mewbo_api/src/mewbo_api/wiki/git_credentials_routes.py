"""Product-wide git credential registry — ``/v1/git/credentials*``.

A credential's *scope* is either a bare host (``git.example.home``, shared by
every repo on that host) or a full slug (``host/owner/repo``, repo-specific) —
see ``mewbo_graph.wiki.credentials`` for the resolution chain that consumes
this store. MewboWiki is the first consumer (onboarding/clone/branches/
freshness all read through ``resolve_chain``); task/vcs-pickup flows are
expected next, which is why this lives at product-wide ``/v1/git/*`` rather
than under ``/v1/wiki/*`` even though it is registered alongside the wiki
routes (the credential store lives in the same ``WikiStoreBase``).

This module is imported only when wiki extras are installed (mirrors
``routes.py``); routes are mounted via ``register(app, runtime)`` called from
``wiki/__init__.py`` right after the wiki blueprint itself.
"""
from __future__ import annotations

import subprocess
from typing import Any

from flask import Blueprint, jsonify, request
from mewbo_core.common import get_logger
from mewbo_graph.wiki.credentials import CredentialScope
from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.types import RepoCredential, WikiError
from pydantic import BaseModel, ConfigDict, Field

from mewbo_api.auth.guard_registry import guard

from .errors import wiki_error_response
from .routes import _pydantic_fields

logging = get_logger(name="api.wiki.git_credentials")

_runtime: Any = None  # populated by register()

# git ls-remote (credential validation) is cheap but not instant on a dead
# host — bounded so a bad scope/host can't hang the request indefinitely.
_VALIDATE_TIMEOUT_SECONDS = 20


def _store() -> WikiStoreBase:
    return _runtime.wiki_store


# ── Wire models (transport-only — never persisted) ───────────────────────────


class CredentialUpsert(BaseModel):
    """``PUT /v1/git/credentials/<scope>`` body — the ONLY direction a secret travels.

    A thin transport shell over the persisted ``RepoCredential``: it carries no
    ``updatedAt`` (the store stamps that at save) and no ``scope`` (that is the
    route param / store key — one binding, never two that can disagree), and
    ``extra="forbid"`` means a client that tries to smuggle either in gets a 400
    rather than having it silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    value: str
    username: str | None = None

    def to_credential(self) -> RepoCredential:
        """Validate into the domain model (empty value / bad kind raise here)."""
        return RepoCredential.model_validate(self.model_dump())


class CredentialValidateRequest(BaseModel):
    """``POST /v1/git/credentials/<scope>/validate`` body."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    repo_url: str | None = Field(default=None, alias="repoUrl")


def _parse_scope(scope: str) -> CredentialScope | None:
    """Validate a ``<path:scope>`` route param, or ``None`` when it is malformed.

    The boundary where a bad scope fails FAST. Previously a malformed scope was
    accepted, written under a key nothing could ever resolve, and only surfaced
    later as an opaque "the clone fell back to anonymous" mystery.
    """
    try:
        return CredentialScope.from_slug(scope)
    except ValueError:
        return None


def _bad_scope_response(scope: str):
    """The 400 a malformed ``<path:scope>`` earns."""
    return wiki_error_response(WikiError(
        code="validation",
        message=(
            f"invalid credential scope {scope!r} — expected a host "
            "(git.example.com) or a host/owner/repo slug"
        ),
        fields={"scope": "invalid"},
    ))


def _is_ssh_url(url: str) -> bool:
    """True when *url* is an SSH-form git remote (``ssh://…`` or ``git@host:path``).

    An SSH key only exercises auth over the SSH transport; an ``https://`` URL
    ignores ``GIT_SSH_COMMAND`` entirely, so validating an SSH key against one
    would run an anonymous HTTPS probe and return a meaningless verdict. Matches
    both the explicit ``ssh://`` scheme and scp-style ``[user@]host:path`` (a
    colon-separated host with no URL scheme and no slash before the colon).
    """
    u = url.strip()
    if u.startswith("ssh://"):
        return True
    if "://" in u:  # any other explicit scheme (https/git/http) is not SSH
        return False
    host_part, sep, path = u.partition(":")
    return bool(sep and path and host_part and "/" not in host_part)


def _credential_to_wire(scope: CredentialScope, cred: RepoCredential) -> dict[str, Any]:
    """Project a stored credential to its wire shape. NEVER includes the value.

    ``scopeType`` reads the model's own ``kind`` — the host-vs-repo rule has ONE
    definition (``CredentialScope``), which this wire field mirrors rather than
    re-deriving from a ``"/" in scope`` check.
    """
    value_hint = "ssh key" if cred.kind == "ssh_key" else f"…{cred.value[-4:]}"
    return {
        "scope": scope.value,
        "scopeType": scope.kind,
        "kind": cred.kind,
        "username": cred.username,
        "valueHint": value_hint,
        "updatedAt": cred.updated_at,
    }


def register(app, runtime) -> None:
    """Mount /v1/git/credentials* on the given Flask app + attach runtime ref."""
    global _runtime
    _runtime = runtime
    app.register_blueprint(_build_blueprint(), url_prefix="/v1/git")


def _build_blueprint() -> Blueprint:
    bp = Blueprint("git_credentials", __name__)

    @bp.route("/credentials", methods=["GET"])
    @guard.requires("git_credentials.read")
    def list_credentials():
        from mewbo_graph.wiki.credentials import CredentialStore  # noqa: PLC0415

        creds = CredentialStore.list(_store())
        out = [
            _credential_to_wire(scope, cred)
            for scope, cred in sorted(creds.items(), key=lambda kv: kv[0].value)
        ]
        return jsonify({"credentials": out})

    @bp.route("/credentials/<path:scope>", methods=["PUT"])
    @guard.requires("git_credentials.write")
    def put_credential(scope: str):
        from mewbo_graph.wiki.credentials import CredentialStore  # noqa: PLC0415

        parsed = _parse_scope(scope)
        if parsed is None:
            return _bad_scope_response(scope)
        try:
            cred = CredentialUpsert.model_validate(
                request.get_json(silent=True) or {}
            ).to_credential()
        except Exception as exc:
            fields = _pydantic_fields(exc)
            return wiki_error_response(
                WikiError(code="validation", message=str(exc), fields=fields or None)
            )
        CredentialStore.save(_store(), parsed, cred)
        return jsonify({"ok": True})

    @bp.route("/credentials/<path:scope>", methods=["DELETE"])
    @guard.requires("git_credentials.write")
    def delete_credential(scope: str):
        from mewbo_graph.wiki.credentials import CredentialStore  # noqa: PLC0415

        parsed = _parse_scope(scope)
        if parsed is None:
            return _bad_scope_response(scope)
        deleted = CredentialStore.delete(_store(), parsed)
        if not deleted:
            return wiki_error_response(
                WikiError(code="not_found", message=f"no credential for scope {parsed}")
            )
        return jsonify({"ok": True})

    @bp.route("/credentials/<path:scope>/validate", methods=["POST"])
    @guard.requires("git_credentials.write")
    def validate_credential(scope: str):
        from mewbo_graph.wiki.credentials import CredentialStore  # noqa: PLC0415

        parsed = _parse_scope(scope)
        if parsed is None:
            return _bad_scope_response(scope)
        cred = CredentialStore.load(_store(), parsed)
        if cred is None:
            return wiki_error_response(
                WikiError(code="not_found", message=f"no credential for scope {parsed}")
            )

        try:
            req = CredentialValidateRequest.model_validate(
                request.get_json(silent=True) or {}
            )
        except Exception as exc:
            fields = _pydantic_fields(exc)
            return wiki_error_response(
                WikiError(code="validation", message=str(exc), fields=fields or None)
            )
        repo_url = (req.repo_url or "").strip()
        if not repo_url:
            # A host scope names no single repo to probe — there is nothing to
            # ls-remote against, so the caller must supply one.
            if parsed.kind == "host":
                return wiki_error_response(WikiError(
                    code="validation",
                    message="repoUrl is required to validate a host-scoped credential",
                    fields={"repoUrl": "required"},
                ))
            repo_url = f"https://{parsed}"

        from mewbo_graph.plugins.wiki.clone import (  # noqa: PLC0415
            _inject_token,
            _is_private_host,
            _redact,
            _ssh_env_for,
            build_ls_remote_command,
            hardened_git_env,
        )

        # An SSH key ONLY exercises auth over the SSH transport — an ``https://``
        # URL ignores ``GIT_SSH_COMMAND``, so probing one would run an anonymous
        # HTTPS ls-remote and hand back a meaningless ``ok`` on a public repo /
        # ``fail`` on a private one. Require an SSH-form URL instead of silently
        # mis-validating (A2). The repo-scope default is ``https://<scope>``, so a
        # host/repo whose credential is an SSH key MUST pass an explicit SSH URL.
        if cred.kind == "ssh_key" and not _is_ssh_url(repo_url):
            return jsonify({
                "ok": False,
                "detail": (
                    "SSH key validation requires an SSH repository URL "
                    "(ssh://… or git@host:owner/repo)"
                ),
            })

        url = repo_url
        env: dict[str, str] | None = None
        key_path = None
        if cred.kind == "token":
            # L3/C1: thread the credential's own username (GitLab oauth2 / deploy
            # tokens) exactly as the clone chain does, else a cred that validates
            # here would auth differently from what actually clones.
            url = _inject_token(repo_url, cred.value, cred.username)
        else:
            env, key_path = _ssh_env_for(cred.value)

        # R3: reuse the shared hardened env + ls-remote argv builders so this
        # route can't drift from the clone/branches/freshness posture — git's OWN
        # credential helper disabled (never touch the read-only mounted
        # ~/.git-credentials, whose EBUSY-on-erase masked real auth errors),
        # GIT_TERMINAL_PROMPT=0, and TLS relaxed only for private-TLD self-signed
        # hosts. ``env`` is the SSH-key env for an ssh_key cred, else None.
        run_env = hardened_git_env(env)
        cmd = build_ls_remote_command(url, "HEAD", private_host=_is_private_host(repo_url))

        try:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=_VALIDATE_TIMEOUT_SECONDS, env=run_env
                )
            except subprocess.TimeoutExpired:
                return jsonify({
                    "ok": False,
                    "detail": f"git ls-remote timed out after {_VALIDATE_TIMEOUT_SECONDS}s",
                })
        finally:
            if key_path is not None:
                key_path.unlink(missing_ok=True)

        if proc.returncode == 0:
            return jsonify({"ok": True, "detail": f"credential authenticated for {parsed}"})
        detail = (proc.stderr or b"").decode(errors="ignore").strip() or "git ls-remote failed"
        # R6: reuse the clone scrubber (guards empty/None secrets too) instead of
        # a bare str.replace, so the value never leaks into a failure detail.
        detail = _redact(detail, [cred.value])
        return jsonify({"ok": False, "detail": detail})

    return bp


__all__ = ["register"]
