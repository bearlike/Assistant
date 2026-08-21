"""HTTP contract for the editable wiki-project settings surface.

``GET /v1/wiki/projects/<slug>/settings`` + ``PATCH /v1/wiki/projects/<slug>``.
Drives the real Flask routes over a temp JsonWikiStore — the guards under test
(dev-mode re-gate, repo-identity, catalog, secret rejection) are the ones a
console edit form would otherwise be able to walk straight through.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mewbo_graph.wiki.types import IndexingJob, Project, ProjectSettings, WizardSubmission

API_KEY = "test-settings-key"
SLUG = "github.com/org/repo"


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def client(monkeypatch, store):
    monkeypatch.setenv("MEWBO_MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask

    runtime = MagicMock()
    runtime.wiki_store = store
    runtime.resolve_session.return_value = "sess-stub"

    app = Flask(__name__)
    app.config["TESTING"] = True
    routes_mod.register(app, runtime)

    yield app.test_client(), store

    routes_mod._runtime = None


@pytest.fixture()
def dev_mode_off(monkeypatch):
    """runtime.developer_mode = False — the default posture."""
    monkeypatch.setattr(
        "mewbo_api.wiki.settings._runtime_developer_mode", lambda: False
    )


@pytest.fixture()
def dev_mode_on(monkeypatch):
    monkeypatch.setattr(
        "mewbo_api.wiki.settings._runtime_developer_mode", lambda: True
    )


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def _submission(**overrides) -> WizardSubmission:
    data = {
        "repoUrl": "https://github.com/org/repo",
        "slug": SLUG,
        "platform": "github",
        "depth": "comprehensive",
        "language": "en",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
    }
    data.update(overrides)
    return WizardSubmission.model_validate(data)


def _seed_git_project(store, **submission_overrides) -> None:
    """A normal git-backed project with a settings record."""
    store.create_project(
        Project(
            slug=SLUG, source="github", lang="en", indexedAt="2026-01-01T00:00:00Z",
            pages=3, desc="upstream blurb", repoUrl="https://github.com/org/repo",
        )
    )
    store.save_project_settings(
        SLUG, ProjectSettings.from_submission(_submission(**submission_overrides))
    )


def _seed_catalog_project(store, slug: str = "catalog/products") -> None:
    """A non-git catalog project: no clone URL, no git submission."""
    store.create_project(
        Project(
            slug=slug, source="git", lang="en", indexedAt="2026-01-01T00:00:00Z",
            pages=2, desc="a product catalog", repoUrl=None,
        )
    )


# ── GET settings ───────────────────────────────────────────────────────────────


def test_get_settings_returns_the_editable_contract(client, store, dev_mode_off) -> None:
    c, st = client
    _seed_git_project(st, ref="develop", depth="concise", dirs=["src"])

    resp = c.get(f"/v1/wiki/projects/{SLUG}/settings", headers=_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["slug"] == SLUG
    assert body["kind"] == "git"
    assert body["model"] == "anthropic/claude-sonnet-4-6"
    assert body["ref"] == "develop"
    assert body["depth"] == "concise"
    assert body["dirs"] == ["src"]
    assert body["graphOnly"] is False
    assert body["embeddingModel"] is None
    assert body["editable"]["embeddingModel"] is True
    # ``editable`` is camelCase like the rest of the DTO — a snake_case key here is
    # one the console literally cannot look up.
    assert body["editable"]["model"] is True
    assert body["editable"]["filterMode"] is True
    # The FE disables the switch rather than offering an edit that would 403.
    assert body["editable"]["graphOnly"] is False


def test_get_settings_reports_credential_presence_never_the_value(client, store) -> None:
    """A credential's SCOPE is surfaced so the console can deep-link Security; the
    secret itself must never appear in any response."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    c, st = client
    _seed_git_project(st)
    CredentialStore.save(
        st,
        CredentialScope.from_slug(SLUG),
        RepoCredential(kind="token", value="ghp_supersecret", username=None),
    )

    resp = c.get(f"/v1/wiki/projects/{SLUG}/settings", headers=_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["credential"] == {
        "present": True,
        "scope": SLUG,
        "scopeType": "repo",
    }
    assert "ghp_supersecret" not in resp.get_data(as_text=True)


def test_get_settings_reports_a_host_scoped_credential(client, store) -> None:
    """The host tier covers every repo on that host — report where it actually lives."""
    from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
    from mewbo_graph.wiki.types import RepoCredential

    c, st = client
    _seed_git_project(st)
    CredentialStore.save(
        st,
        CredentialScope.from_slug("github.com"),
        RepoCredential(kind="token", value="ghp_shared", username=None),
    )

    body = c.get(f"/v1/wiki/projects/{SLUG}/settings", headers=_headers()).get_json()

    assert body["credential"] == {
        "present": True,
        "scope": "github.com",
        "scopeType": "host",
    }


def test_get_settings_without_a_credential(client, store) -> None:
    c, st = client
    _seed_git_project(st)

    body = c.get(f"/v1/wiki/projects/{SLUG}/settings", headers=_headers()).get_json()

    assert body["credential"] == {"present": False, "scope": None, "scopeType": None}


def test_get_settings_reconstructs_for_a_legacy_project(client, store) -> None:
    """A project first indexed before the record existed still shows real settings —
    read off the newest job submission, which is what a refresh would replay."""
    c, st = client
    st.create_project(
        Project(
            slug=SLUG, source="github", lang="en", indexedAt="2026-01-01T00:00:00Z",
            pages=3, desc="d", repoUrl="https://github.com/org/repo",
        )
    )
    st.create_job(
        IndexingJob(
            job_id="j1", slug=SLUG, status="complete", scanned_count=1, total_count=1,
            current_file=None, phase_started_at="2026-01-01T00:00:00Z",
        )
    )
    st.save_job_submission("j1", _submission(model="legacy/model").model_dump(by_alias=True))
    assert st.get_project_settings(SLUG) is None  # no record yet

    body = c.get(f"/v1/wiki/projects/{SLUG}/settings", headers=_headers()).get_json()

    assert body["model"] == "legacy/model"


def test_get_settings_unknown_project_404(client, store) -> None:
    c, _ = client
    resp = c.get("/v1/wiki/projects/no/such/settings", headers=_headers())
    assert resp.status_code == 404


def test_get_settings_catalog_project_reduced_shape(client, store) -> None:
    c, st = client
    _seed_catalog_project(st)

    resp = c.get("/v1/wiki/projects/catalog/products/settings", headers=_headers())

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["kind"] == "catalog"
    assert body["desc"] == "a product catalog"
    assert body["editable"]["desc"] is True
    assert body["editable"]["ref"] is False
    assert body["editable"]["graphOnly"] is False
    assert "model" not in body


def test_get_settings_requires_auth(client, store) -> None:
    c, st = client
    _seed_git_project(st)
    assert c.get(f"/v1/wiki/projects/{SLUG}/settings").status_code == 401


# ── PATCH ──────────────────────────────────────────────────────────────────────


def test_patch_updates_settings_and_returns_the_dto(client, store, dev_mode_off) -> None:
    c, st = client
    _seed_git_project(st)

    resp = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"model": "openai/gpt-5.4", "ref": "develop", "depth": "concise"},
        headers=_headers(),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["model"] == "openai/gpt-5.4"
    assert body["ref"] == "develop"
    assert body["depth"] == "concise"
    # Persisted — this is what the next index replays.
    saved = st.get_project_settings(SLUG)
    assert saved.model == "openai/gpt-5.4"
    assert saved.ref == "develop"


def test_patch_is_partial_omitted_fields_persist(client, store, dev_mode_off) -> None:
    c, st = client
    _seed_git_project(st, ref="develop", dirs=["src"])

    c.patch(f"/v1/wiki/projects/{SLUG}", json={"model": "openai/gpt-5.4"}, headers=_headers())

    saved = st.get_project_settings(SLUG)
    assert saved.ref == "develop"
    assert saved.dirs == ["src"]


def test_patch_explicit_null_ref_clears_the_pinned_branch(client, store, dev_mode_off) -> None:
    """Omitted ≠ null: an explicit null unpins the branch (back to the repo default)."""
    c, st = client
    _seed_git_project(st, ref="develop")

    body = c.patch(
        f"/v1/wiki/projects/{SLUG}", json={"ref": None}, headers=_headers()
    ).get_json()

    assert body["ref"] is None
    assert st.get_project_settings(SLUG).ref is None


def test_patch_embedding_model_set_omit_and_clear_are_distinct(client, store, dev_mode_off) -> None:
    """One project can select a vector model without a partial PATCH clearing it.

    A project needs a full rebuild when its embedding model changes, so an
    accidental clear would make a later refresh use the deployment default and
    rebuild the wrong vectors. Omission must preserve the selected override;
    explicit null is the intentional way to return to that default.
    """
    c, st = client
    _seed_git_project(st)

    set_body = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"embeddingModel": "openai/text-embedding-3-large"},
        headers=_headers(),
    ).get_json()
    assert set_body["embeddingModel"] == "openai/text-embedding-3-large"
    assert st.get_project_settings(SLUG).embedding_model == "openai/text-embedding-3-large"

    c.patch(f"/v1/wiki/projects/{SLUG}", json={"ref": "develop"}, headers=_headers())
    assert st.get_project_settings(SLUG).embedding_model == "openai/text-embedding-3-large"

    cleared = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"embeddingModel": None},
        headers=_headers(),
    ).get_json()
    assert cleared["embeddingModel"] is None
    assert st.get_project_settings(SLUG).embedding_model is None


def test_patch_desc_applies_immediately_and_survives_reindex(
    client, store, dev_mode_off
) -> None:
    """desc is the one display field: written through to the Project snapshot AND
    persisted as the override finalize reads back."""
    c, st = client
    _seed_git_project(st)

    c.patch(f"/v1/wiki/projects/{SLUG}", json={"desc": "my blurb"}, headers=_headers())

    assert st.get_project(SLUG).desc == "my blurb"       # visible now
    assert st.get_project_settings(SLUG).desc == "my blurb"  # survives the next index


def test_patch_rejects_a_token(client, store, dev_mode_off) -> None:
    """A secret must never enter this record — credentials have ONE registry."""
    c, st = client
    _seed_git_project(st)

    resp = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"model": "openai/gpt-5.4", "token": "ghp_secret"},
        headers=_headers(),
    )

    assert resp.status_code == 400
    assert st.get_project_settings(SLUG).model == "anthropic/claude-sonnet-4-6"  # unchanged


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "other/repo"},          # rename — the store key, immutable
        {"pages": 99},                   # system-owned
        {"landingPageId": "evil"},       # system-owned
        {"commitSha": "deadbeef"},       # system-owned
        {"nonsense": 1},                 # unknown
    ],
)
def test_patch_forbids_unknown_and_system_owned_fields(client, store, dev_mode_off, body) -> None:
    """extra="forbid" — silently ignoring these is how a client thinks it renamed a project."""
    c, st = client
    _seed_git_project(st)

    resp = c.patch(f"/v1/wiki/projects/{SLUG}", json=body, headers=_headers())

    assert resp.status_code == 400


def test_patch_graph_only_is_403_without_developer_mode(client, store, dev_mode_off) -> None:
    """THE privilege escalation this route must not open.

    graph-only is gated at POST /index and is STICKY thereafter (refresh replays it
    unchecked), so a PATCH that wrote it blind would hand an unprivileged caller the
    zero-LLM/no-docs path the index route refuses them.
    """
    c, st = client
    _seed_git_project(st)

    resp = c.patch(f"/v1/wiki/projects/{SLUG}", json={"graphOnly": True}, headers=_headers())

    assert resp.status_code == 403
    assert st.get_project_settings(SLUG).graph_only is False  # never persisted


def test_patch_graph_only_allowed_in_developer_mode(client, store, dev_mode_on) -> None:
    c, st = client
    _seed_git_project(st)

    resp = c.patch(f"/v1/wiki/projects/{SLUG}", json={"graphOnly": True}, headers=_headers())

    assert resp.status_code == 200
    assert resp.get_json()["graphOnly"] is True
    assert st.get_project_settings(SLUG).graph_only is True


def test_patch_turning_graph_only_off_is_not_privileged(client, store, dev_mode_off) -> None:
    """Restoring documentation is not a privileged action — only enabling is."""
    c, st = client
    _seed_git_project(st, graphOnly=True)

    resp = c.patch(f"/v1/wiki/projects/{SLUG}", json={"graphOnly": False}, headers=_headers())

    assert resp.status_code == 200
    assert st.get_project_settings(SLUG).graph_only is False


def test_patch_repo_identity_change_is_409(client, store, dev_mode_off) -> None:
    """A bare URL swap would leave pages/graph/credentials pinned to the OLD repo."""
    c, st = client
    _seed_git_project(st)

    resp = c.patch(
        f"/v1/wiki/projects/{SLUG}",
        json={"repoUrl": "https://github.com/someone-else/other-repo"},
        headers=_headers(),
    )

    assert resp.status_code == 409
    assert st.get_project_settings(SLUG).repo_url == "https://github.com/org/repo"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/org/repo.git",   # .git suffix
        "https://github.com/org/repo/",      # trailing slash
        "http://GitHub.com/org/repo",        # scheme + host case
    ],
)
def test_patch_same_repo_normalisation_is_allowed(client, store, dev_mode_off, url) -> None:
    """Only a change of (host, owner, repo) is an identity change; re-writing the
    same repo's URL in another form is a legitimate correction."""
    c, st = client
    _seed_git_project(st)

    resp = c.patch(f"/v1/wiki/projects/{SLUG}", json={"repoUrl": url}, headers=_headers())

    assert resp.status_code == 200
    assert st.get_project_settings(SLUG).repo_url == url


def test_patch_unknown_project_404(client, store, dev_mode_off) -> None:
    c, _ = client
    resp = c.patch("/v1/wiki/projects/no/such", json={"model": "m"}, headers=_headers())
    assert resp.status_code == 404


def test_patch_catalog_project_rejects_index_time_fields_410(
    client, store, dev_mode_off
) -> None:
    """A catalog project has no clone/scan/graph run, so a ref or index model would
    be a setting nothing ever reads."""
    c, st = client
    _seed_catalog_project(st)

    resp = c.patch(
        "/v1/wiki/projects/catalog/products",
        json={"ref": "main", "filterMode": "include"},
        headers=_headers(),
    )

    assert resp.status_code == 410
    # The error names the offending fields the way the CLIENT spelled them.
    assert set(resp.get_json()["fields"]) == {"ref", "filterMode"}


def test_patch_catalog_project_can_still_edit_desc(client, store, dev_mode_off) -> None:
    """desc is pure display — editable on any project."""
    c, st = client
    _seed_catalog_project(st)

    resp = c.patch(
        "/v1/wiki/projects/catalog/products",
        json={"desc": "curated product docs"},
        headers=_headers(),
    )

    assert resp.status_code == 200
    assert st.get_project("catalog/products").desc == "curated product docs"


def test_patch_does_not_start_a_reindex(client, store, dev_mode_off) -> None:
    """Settings take effect on the NEXT index — a PATCH must not queue one (which is
    also why it can't be used to bypass the per-IP indexing rate limiter)."""
    c, st = client
    _seed_git_project(st)

    c.patch(f"/v1/wiki/projects/{SLUG}", json={"model": "openai/gpt-5.4"}, headers=_headers())

    assert st.list_jobs(slug=SLUG) == []


def test_patch_requires_auth(client, store) -> None:
    c, st = client
    _seed_git_project(st)
    assert c.patch(f"/v1/wiki/projects/{SLUG}", json={"model": "m"}).status_code == 401


# ── delete cascades the settings record ───────────────────────────────────────


def test_delete_project_drops_the_settings_record(client, store) -> None:
    """A re-created slug must not inherit the dead project's model/ref/graph-only."""
    c, st = client
    _seed_git_project(st, graphOnly=False, ref="develop")

    resp = c.delete(f"/v1/wiki/projects/{SLUG}", headers=_headers())

    assert resp.status_code == 200
    assert st.get_project_settings(SLUG) is None
