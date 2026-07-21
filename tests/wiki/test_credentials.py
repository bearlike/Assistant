"""Tests for the git-auth domain model: CredentialScope / CredentialSource /
CredentialCandidate / RepoCredential, the credential store, and resolve_chain."""
from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.wiki.credentials import CredentialScope
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import RepoCredential
from pydantic import ValidationError


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def S(value: str) -> CredentialScope:
    """Shorthand for a validated scope (the store facade is scope-typed)."""
    return CredentialScope.from_slug(value)


def test_token_credential_roundtrips() -> None:
    cred = RepoCredential(kind="token", value="ghp_secret", username=None)
    dumped = cred.model_dump(mode="json")
    assert dumped == {
        "kind": "token",
        "value": "ghp_secret",
        "username": None,
        "updated_at": None,
    }
    assert RepoCredential.model_validate(dumped) == cred


def test_ssh_key_credential_roundtrips() -> None:
    cred = RepoCredential(
        kind="ssh_key", value="-----BEGIN KEY-----\nabc\n-----END KEY-----", username="git"
    )
    assert RepoCredential.model_validate(cred.model_dump(mode="json")) == cred


def test_empty_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RepoCredential(kind="token", value="", username=None)


def test_whitespace_only_value_is_rejected() -> None:
    """Stripping happens BEFORE the not-empty check, so "  " is empty."""
    with pytest.raises(ValidationError):
        RepoCredential(kind="token", value="   \n", username=None)


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RepoCredential.model_validate({"kind": "password", "value": "x", "username": None})


def test_extra_field_is_forbidden() -> None:
    with pytest.raises(ValidationError):
        RepoCredential.model_validate(
            {"kind": "token", "value": "x", "username": None, "extra": 1}
        )


# ── RepoCredential.value: the trailing-newline footgun ───────────────────────


def test_token_value_strips_surrounding_whitespace() -> None:
    """A PAT pasted with a trailing newline silently 401s — strip it at definition."""
    cred = RepoCredential(kind="token", value="  ghp_secret\n")
    assert cred.value == "ghp_secret"


def test_ssh_key_value_keeps_internal_newlines() -> None:
    """Only the SURROUNDING whitespace goes — a PEM's internal newlines are the key.

    (``clone._ssh_env_for`` re-appends the terminating newline the key file needs.)
    """
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nbody\n-----END OPENSSH PRIVATE KEY-----"
    cred = RepoCredential(kind="ssh_key", value=f"\n{pem}\n\n")
    assert cred.value == pem
    assert "\n" in cred.value


def test_dedup_key_includes_username() -> None:
    """A shared value under two usernames authenticates differently — both are kept."""
    a = RepoCredential(kind="token", value="shared", username="alice")
    b = RepoCredential(kind="token", value="shared", username="bob")
    assert a.dedup_key != b.dedup_key
    assert a.dedup_key == ("token", "shared", "alice")


# ── CredentialScope: parsing + normalisation ─────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Scheme URLs.
        ("https://git.home/org/repo", "git.home/org/repo"),
        ("https://git.home/org/repo.git", "git.home/org/repo"),
        ("ssh://git@git.home:2222/org/repo", "git.home/org/repo"),
        ("https://git.home", "git.home"),
        # scp-style remotes — the shape the wizard accepts before a slug exists.
        ("git@github.com:owner/repo.git", "github.com/owner/repo"),
        ("github.com:owner/repo", "github.com/owner/repo"),
        # Bare slugs + hosts.
        ("git.home/org/repo", "git.home/org/repo"),
        ("git.home", "git.home"),
        # Normalisation: trim, trailing slash, trailing .git, host lowercased.
        ("  git.home/org/repo  ", "git.home/org/repo"),
        ("git.home/org/repo/", "git.home/org/repo"),
        ("git.home/org/repo.git/", "git.home/org/repo"),
        ("GIT.HOME/Org/Repo", "git.home/Org/Repo"),  # owner/repo case is preserved
        ("GIT.HOME", "git.home"),
    ],
)
def test_scope_parses_and_normalises_every_shape(raw: str, expected: str) -> None:
    assert CredentialScope.from_repo_url(raw).value == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://git.home/org/repo",  # a URL is not a store key — use from_repo_url
        "git.home//repo",  # empty inner segment
        "git home/org",  # whitespace
        "git@github.com:owner/repo",  # an un-parsed scp remote leaking in as a key
    ],
)
def test_scope_rejects_malformed(raw: str) -> None:
    """A malformed scope fails FAST — it used to be written under an unresolvable key."""
    with pytest.raises(ValidationError):
        CredentialScope.from_slug(raw)


def test_scope_coerce_is_tolerant_on_read_paths() -> None:
    """READ paths degrade to "no scope" rather than blowing up a would-be clone."""
    assert CredentialScope.coerce("") is None
    assert CredentialScope.coerce(None) is None
    assert CredentialScope.coerce("git home/org") is None
    assert CredentialScope.coerce("git.home/org/repo") == S("git.home/org/repo")
    scope = S("git.home")
    assert CredentialScope.coerce(scope) is scope  # already a scope → identity


def test_scope_is_frozen_and_forbids_extras() -> None:
    scope = S("git.home")
    with pytest.raises(ValidationError):
        scope.value = "other.home"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        CredentialScope.model_validate({"value": "git.home", "kind": "host"})


# ── CredentialScope: kind / host / owner / repo ──────────────────────────────


def test_host_scope_shape() -> None:
    scope = S("git.home")
    assert scope.kind == "host"
    assert scope.host == "git.home"
    assert scope.owner is None
    assert scope.repo is None
    assert scope.host_scope() == scope  # a host scope is its own parent


def test_repo_scope_shape() -> None:
    scope = S("git.home/org/repo")
    assert scope.kind == "repo"
    assert scope.host == "git.home"
    assert (scope.owner, scope.repo) == ("org", "repo")
    assert scope.host_scope() == S("git.home")


def test_legacy_two_segment_scope_still_parses() -> None:
    """``owner/repo`` (pre-host slugs) is still stored in the wild — keep it valid."""
    scope = S("org/repo")
    assert scope.kind == "repo"
    assert (scope.owner, scope.repo) == ("org", "repo")


def test_deep_scope_is_a_repo_scope_not_a_rejection() -> None:
    """A GitLab SUBGROUP is a legitimate 4-segment slug — rejecting it would make
    that repo's credential unresolvable and silently downgrade its clone to
    anonymous. owner/repo read the LAST TWO segments."""
    scope = S("gitlab.com/group/subgroup/project")
    assert scope.kind == "repo"
    assert scope.host == "gitlab.com"
    assert (scope.owner, scope.repo) == ("subgroup", "project")
    assert scope.host_scope() == S("gitlab.com")


# ── CredentialScope.covers: THE sharing rule ─────────────────────────────────


def test_host_scope_covers_every_repo_on_that_host() -> None:
    host = S("git.home")
    assert host.covers(S("git.home/org/repo")) is True
    assert host.covers(S("git.home/other/thing")) is True
    assert host.covers(host) is True


def test_repo_scope_covers_only_itself() -> None:
    repo = S("git.home/org/repo")
    assert repo.covers(repo) is True
    assert repo.covers(S("git.home/org/other")) is False
    assert repo.covers(S("git.home")) is False  # a repo cred never authenticates the host


def test_scope_never_covers_a_different_host() -> None:
    """A credential can never leak across hosts — this is the security half of the rule."""
    assert S("git.home").covers(S("evil.home/org/repo")) is False
    assert S("git.home/org/repo").covers(S("evil.home/org/repo")) is False


# ── CredentialCandidate: the anonymous invariant ─────────────────────────────


def test_anonymous_candidate_must_not_carry_a_credential() -> None:
    from mewbo_graph.wiki.credentials import CredentialCandidate, CredentialSource

    with pytest.raises(ValidationError):
        CredentialCandidate(
            source=CredentialSource.ANONYMOUS,
            credential=RepoCredential(kind="token", value="x"),
        )


def test_non_anonymous_candidate_requires_a_credential() -> None:
    from mewbo_graph.wiki.credentials import CredentialCandidate, CredentialSource

    with pytest.raises(ValidationError):
        CredentialCandidate(source=CredentialSource.STORE_REPO, credential=None)


def test_candidate_accessors_project_the_credential() -> None:
    from mewbo_graph.wiki.credentials import CredentialCandidate, CredentialSource

    token = CredentialCandidate(
        source=CredentialSource.STORE_HOST,
        credential=RepoCredential(kind="token", value="tok", username="oauth2"),
    )
    assert (token.is_token, token.token, token.ssh_key, token.username) == (
        True, "tok", None, "oauth2",
    )

    key = CredentialCandidate(
        source=CredentialSource.STORE_REPO,
        credential=RepoCredential(kind="ssh_key", value="KEY", username="git"),
    )
    # An ssh key is NOT a token, and its username must never leak into the
    # token-injection path.
    assert (key.is_token, key.token, key.ssh_key, key.username) == (False, None, "KEY", None)

    anon = CredentialCandidate(source=CredentialSource.ANONYMOUS)
    assert (anon.is_token, anon.token, anon.ssh_key, anon.username) == (False, None, None, None)


def test_source_scope_for_names_only_the_stored_tiers() -> None:
    """Only a STORED candidate names a scope a user can go fix."""
    from mewbo_graph.wiki.credentials import CredentialSource

    scope = S("git.home/org/repo")
    assert CredentialSource.STORE_REPO.scope_for(scope) == scope
    assert CredentialSource.STORE_HOST.scope_for(scope) == S("git.home")
    assert CredentialSource.ARG.scope_for(scope) is None
    assert CredentialSource.AMBIENT.scope_for(scope) is None
    assert CredentialSource.ANONYMOUS.scope_for(scope) is None


# ── CredentialStore ──────────────────────────────────────────────────────────


def test_json_store_credential_crud(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_credentials("org/repo") is None
    cred = RepoCredential(kind="token", value="ghp_x", username=None)
    store.save_credentials("org/repo", cred.model_dump(mode="json"))
    assert store.get_credentials("org/repo") == cred.model_dump(mode="json")
    assert store.delete_credentials("org/repo") is True
    assert store.delete_credentials("org/repo") is False
    assert store.get_credentials("org/repo") is None


def test_json_store_credential_file_is_0600(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_credentials("org/repo", {"kind": "token", "value": "s", "username": None})
    cred_file = tmp_path / "credentials" / "org__repo.json"
    assert cred_file.exists()
    assert (cred_file.stat().st_mode & 0o777) == 0o600


def test_json_store_credentials_isolated_dir(tmp_path: Path) -> None:
    """Credentials live in their OWN subdir, never alongside job submissions."""
    store = _store(tmp_path)
    store.save_credentials("org/repo", {"kind": "token", "value": "s", "username": None})
    assert (tmp_path / "credentials").is_dir()
    # Token text must not appear anywhere under jobs/ or projects/.
    for sub in ("jobs", "projects"):
        for f in (tmp_path / sub).rglob("*"):
            if f.is_file():
                assert "s" not in f.read_text() or "value" not in f.read_text()


def test_credential_store_save_load_delete(tmp_path: Path) -> None:
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    cred = RepoCredential(kind="token", value="ghp_x", username=None)
    assert CredentialStore.load(store, S("org/repo")) is None
    CredentialStore.save(store, S("org/repo"), cred)
    loaded = CredentialStore.load(store, S("org/repo"))
    # ``save`` stamps ``updated_at``, so the round-trip matches on the caller's
    # fields but additionally carries the audit timestamp.
    assert loaded is not None
    assert (loaded.kind, loaded.value, loaded.username) == (
        cred.kind, cred.value, cred.username,
    )
    assert loaded.updated_at is not None
    assert CredentialStore.delete(store, S("org/repo")) is True
    assert CredentialStore.load(store, S("org/repo")) is None


def test_credential_store_normalises_the_key(tmp_path: Path) -> None:
    """A scope saved from a copy-pasted remote URL resolves under the SLUG the
    chain looks up — otherwise the credential is stored but never found."""
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    CredentialStore.save(
        store,
        CredentialScope.from_repo_url("https://GIT.HOME/org/repo.git"),
        RepoCredential(kind="token", value="tok"),
    )
    assert CredentialStore.load(store, S("git.home/org/repo")) is not None


def test_credential_store_encode_is_identity_today(tmp_path: Path) -> None:
    """The on-disk blob equals the loaded model's dump PLUS a ``scope`` sidecar.

    ``save`` stamps ``updated_at`` (a durable audit field) and the ``scope`` key
    (so ``list_credentials`` recovers the exact scope without a lossy filename
    reversal — L4). The credential seam itself is still identity: strip the
    ``scope`` sidecar and the blob equals the loaded model's dump.
    """
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    cred = RepoCredential(kind="ssh_key", value="KEYDATA", username="git")
    CredentialStore.save(store, S("org/repo"), cred)
    raw = store.get_credentials("org/repo")
    loaded = CredentialStore.load(store, S("org/repo"))
    assert loaded is not None
    assert raw is not None
    assert raw["scope"] == "org/repo"
    assert {k: v for k, v in raw.items() if k != "scope"} == loaded.model_dump(mode="json")
    assert raw["kind"] == "ssh_key" and raw["value"] == "KEYDATA"


def test_credential_store_load_ignores_malformed_blob(tmp_path: Path) -> None:
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    store.save_credentials("org/repo", {"kind": "token"})  # missing value
    assert CredentialStore.load(store, S("org/repo")) is None


def test_credential_store_list_returns_every_scope(tmp_path: Path) -> None:
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    CredentialStore.save(
        store, S("git.home/org/repo"), RepoCredential(kind="token", value="a")
    )
    CredentialStore.save(
        store, S("git.home"), RepoCredential(kind="ssh_key", value="KEY", username="git")
    )
    listed = CredentialStore.list(store)
    assert set(listed) == {S("git.home/org/repo"), S("git.home")}
    assert listed[S("git.home/org/repo")].kind == "token"
    assert listed[S("git.home")].kind == "ssh_key"
    # The keys are scopes, so the sharing rule is readable straight off the list.
    assert S("git.home").covers(S("git.home/org/repo")) is True


def test_credential_store_list_recovers_scope_with_literal_double_underscore(
    tmp_path: Path,
) -> None:
    """L4: a scope whose owner/repo contains a literal ``__`` round-trips intact.

    The old filename-derived recovery (``__``→``/``) corrupted such a scope into
    an extra path segment; reading the in-blob ``scope`` field recovers it exactly.
    """
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    scope = S("git.home/org/we__ird")
    CredentialStore.save(store, scope, RepoCredential(kind="token", value="tok"))
    listed = CredentialStore.list(store)
    assert scope in listed
    assert S("git.home/org/we/ird") not in listed  # the corrupted form must be gone
    assert listed[scope].value == "tok"


def test_credential_store_save_stamps_updated_at(tmp_path: Path) -> None:
    """``save`` stamps an ISO-8601 ``updated_at`` even when the caller omits it."""
    from mewbo_graph.wiki.credentials import CredentialStore

    store = _store(tmp_path)
    CredentialStore.save(store, S("org/repo"), RepoCredential(kind="token", value="x"))
    loaded = CredentialStore.load(store, S("org/repo"))
    assert loaded is not None and loaded.updated_at is not None
    assert "T" in loaded.updated_at  # ISO-8601 date/time separator


# ── resolve_chain: the canonical git-auth precedence ─────────────────────────


def test_resolve_chain_full_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """arg → store:repo → store:host → ambient → anonymous, in that order."""
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import CredentialStore, resolve_chain

    store = _store(tmp_path)
    slug = "git.home/org/repo"
    CredentialStore.save(store, S(slug), RepoCredential(kind="token", value="repo_tok"))
    CredentialStore.save(store, S("git.home"), RepoCredential(kind="token", value="host_tok"))
    # Override the conftest autouse (ambient→None) so the ambient tier appears.
    monkeypatch.setattr(
        cred_mod, "ambient_credential",
        lambda host: RepoCredential(kind="token", value="ambient_tok"),
    )

    chain = list(resolve_chain(store, slug, arg_token="arg_tok"))
    assert [c.source for c in chain] == [
        "arg", "store:repo", "store:host", "ambient", "anonymous",
    ]
    assert [c.token for c in chain] == [
        "arg_tok", "repo_tok", "host_tok", "ambient_tok", None,
    ]
    assert chain[-1].credential is None  # anonymous is always the terminal candidate


def test_resolve_chain_accepts_a_scope_or_a_raw_slug(tmp_path: Path) -> None:
    """The chain is scope-typed but still takes the raw slug every job carries."""
    from mewbo_graph.wiki.credentials import CredentialStore, resolve_chain

    store = _store(tmp_path)
    CredentialStore.save(
        store, S("git.home/org/repo"), RepoCredential(kind="token", value="tok")
    )
    by_str = [c.token for c in resolve_chain(store, "git.home/org/repo")]
    by_scope = [c.token for c in resolve_chain(store, S("git.home/org/repo"))]
    assert by_str == by_scope == ["tok", None]


def test_resolve_chain_uses_host_scope_when_no_repo(tmp_path: Path) -> None:
    """With only a host-scoped credential, that shared entry is used (ambient off)."""
    from mewbo_graph.wiki.credentials import CredentialStore, resolve_chain

    store = _store(tmp_path)
    CredentialStore.save(store, S("git.home"), RepoCredential(kind="token", value="host_tok"))
    chain = list(resolve_chain(store, "git.home/org/repo"))  # ambient stubbed None (autouse)
    assert [c.source for c in chain] == ["store:host", "anonymous"]
    assert chain[0].token == "host_tok"


def test_resolve_chain_dedups_identical_repo_and_host_value(tmp_path: Path) -> None:
    """A host credential identical to the repo one is dropped (dedup by kind+value)."""
    from mewbo_graph.wiki.credentials import CredentialStore, resolve_chain

    store = _store(tmp_path)
    CredentialStore.save(
        store, S("git.home/org/repo"), RepoCredential(kind="token", value="same")
    )
    CredentialStore.save(store, S("git.home"), RepoCredential(kind="token", value="same"))
    chain = list(resolve_chain(store, "git.home/org/repo"))
    # store:host dedups against store:repo — only the repo entry + anonymous remain.
    assert [c.source for c in chain] == ["store:repo", "anonymous"]


def test_resolve_chain_ambient_fallback_when_store_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stored credential → the ambient git credential rescues the chain."""
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import resolve_chain

    store = _store(tmp_path)
    monkeypatch.setattr(
        cred_mod, "ambient_credential",
        lambda host: RepoCredential(kind="token", value="amb"),
    )
    chain = list(resolve_chain(store, "git.home/org/repo"))
    assert [c.source for c in chain] == ["ambient", "anonymous"]
    assert chain[0].token == "amb"


def test_resolve_chain_anonymous_only_when_nothing_resolves(tmp_path: Path) -> None:
    """No arg, no store, ambient stubbed off → just the terminal anonymous entry."""
    from mewbo_graph.wiki.credentials import resolve_chain

    store = _store(tmp_path)
    chain = list(resolve_chain(store, "git.home/org/repo"))
    assert [c.source for c in chain] == ["anonymous"]
    assert chain[0].credential is None


def test_resolve_chain_malformed_scope_degrades_to_anonymous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A READ path never raises on a bad slug — it resolves anonymously (and never
    forks the ambient ``git credential fill``, which has no host to ask for)."""
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import resolve_chain

    monkeypatch.setattr(
        cred_mod, "ambient_credential",
        lambda host: pytest.fail("ambient must not be probed without a scope"),
    )
    chain = list(resolve_chain(_store(tmp_path), "not a slug"))
    assert [c.source for c in chain] == ["anonymous"]


def test_resolve_chain_storeless_yields_anonymous_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store-less probe must not pay for the 10s ambient fork either."""
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import resolve_chain

    monkeypatch.setattr(
        cred_mod, "ambient_credential",
        lambda host: pytest.fail("ambient must not be probed without a store"),
    )
    chain = list(resolve_chain(None, "git.home/org/repo"))
    assert [c.source for c in chain] == ["anonymous"]


def test_resolve_chain_is_lazy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ambient fork runs ONLY when iteration actually reaches that tier — a
    consumer satisfied by the first candidate must never pay for it."""
    from mewbo_graph.wiki import credentials as cred_mod
    from mewbo_graph.wiki.credentials import CredentialStore, resolve_chain

    store = _store(tmp_path)
    CredentialStore.save(
        store, S("git.home/org/repo"), RepoCredential(kind="token", value="repo_tok")
    )
    probes: list[str] = []
    monkeypatch.setattr(
        cred_mod, "ambient_credential",
        lambda host: probes.append(host) or RepoCredential(kind="token", value="amb"),
    )

    chain = resolve_chain(store, "git.home/org/repo")
    first = next(chain)
    assert first.token == "repo_tok"
    assert probes == []  # stopped at the winner — the ambient fork never ran


def test_resolve_chain_keeps_same_value_different_username(tmp_path: Path) -> None:
    """C5: two credentials sharing a value but differing in username are BOTH kept."""
    from mewbo_graph.wiki.credentials import CredentialStore, resolve_chain

    store = _store(tmp_path)
    CredentialStore.save(
        store,
        S("git.home/org/repo"),
        RepoCredential(kind="token", value="shared", username="alice"),
    )
    CredentialStore.save(
        store, S("git.home"), RepoCredential(kind="token", value="shared", username="bob")
    )
    chain = list(resolve_chain(store, "git.home/org/repo"))  # ambient stubbed None (autouse)
    # Same value, different username → the dedup key includes username, so the
    # host candidate is NOT dropped against the repo one.
    assert [c.source for c in chain] == ["store:repo", "store:host", "anonymous"]
    assert [c.username for c in chain] == ["alice", "bob", None]


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("fatal: Authentication failed for 'https://git.home/o/r'", True),
        ("remote: HTTP Basic: Access denied", True),
        ("The requested URL returned error: 403", True),
        ("error: The requested URL returned error: 401 Unauthorized", True),
        ("fatal: could not read Username for 'https://git.home': terminal prompts disabled", True),
        ("remote: Repository not found.", True),
        (
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository.",
            True,
        ),
        ("fatal: unable to access '...': Could not resolve host: git.home", False),
        ("fatal: the remote end hung up unexpectedly", False),
        # L1 regression: a bare "403"/"401" substring in a NETWORK error must
        # NOT read as an auth failure (this is what misfired the chain before).
        (
            "fatal: unable to connect to git.home:\n"
            "git.home[0:1.2.3.4]: errno=Connection refused port 8403",
            False,
        ),
        ("fatal: unable to access '...': The requested URL returned error: 502", False),
    ],
)
def test_is_auth_failure_classifies(stderr: str, expected: bool) -> None:
    from mewbo_graph.wiki.credentials import is_auth_failure

    assert is_auth_failure(stderr) is expected


def test_recovery_counter_increments_and_isolated(tmp_path: Path) -> None:
    """The slug-keyed recovery counter increments, is per-slug, and lives on its
    own surface (never the submission sidecar)."""
    store = _store(tmp_path)
    assert store.get_recovery_attempts("org/repo") == 0
    assert store.bump_recovery_attempts("org/repo") == 1
    assert store.bump_recovery_attempts("org/repo") == 2
    assert store.get_recovery_attempts("org/repo") == 2
    # A different slug has its own independent counter.
    assert store.get_recovery_attempts("org/other") == 0
    # The submission sidecar is untouched by the counter writes.
    store.save_job_submission("j1", {"slug": "org/repo", "dirs": ["src"]})
    store.bump_recovery_attempts("org/repo")
    assert store.get_job_submission("j1") == {"slug": "org/repo", "dirs": ["src"]}
