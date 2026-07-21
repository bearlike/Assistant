"""HTTP contract tests for the custom-system-instructions REST surface.

Exercises the operator-only CRUD + preview routes over the singleton
``SystemInstructionsDoc``. Swaps the module store to a tmp-backed instance
(mirrors ``test_triggers_routes._setup``) via the ONE registered
``SystemInstructionsRoutesController`` the import-time
``init_system_instructions_routes`` wired — no module-level collaborator
globals, so a test points it at a fresh store by reassigning its field.
"""

# mypy: ignore-errors

import time
from types import SimpleNamespace

from mewbo_api.system_instructions import InstructionValueSources, routes as si_routes
from mewbo_core.session_provenance import SessionOrigin
from mewbo_core.system_instructions import GLOBAL_INSTRUCTIONS_ID, InstructionContext
from mewbo_core.system_instructions.store import JsonSystemInstructionsStore


def _setup(tmp_path, *, value_sources=None):
    """Point the registered controller at a fresh tmp-backed store (+ optional sources).

    The ONE registered controller is repointed by reassigning its fields — the
    same seam ``test_triggers_routes`` uses, and the reason the module holds no
    collaborator globals.
    """
    store = JsonSystemInstructionsStore(data_file=str(tmp_path / "system_instructions.json"))
    controller = si_routes._controller
    assert controller is not None  # set by init_system_instructions_routes at backend import
    controller.store = store
    if value_sources is not None:
        controller.value_sources = value_sources
    return store


PATH = "/api/system-instructions"


# ---------------------------------------------------------------------------
# Value-source fakes — the REAL InstructionValueSources over stubbed I/O.
#
# Only the I/O boundaries are faked (the LLM proxy call, the plugin/registry
# discovery, the project store): the probe fan-out, the per-probe failure
# isolation and the catalog projection are the production code paths.
# ---------------------------------------------------------------------------


class _FakeLlm:
    """``config.llm`` — ``list_models`` is the ONE live HTTP call in the feature."""

    def __init__(self, models=("gpt-5.4", "claude-opus-4-8"), error=None):
        self.models = list(models)
        self.error = error

    def list_models(self, *, timeout=8.0):
        if self.error is not None:
            raise self.error
        return list(self.models)


def _fake_config(*, projects=(("mewbo", "/srv/mewbo"),), llm=None):
    """``AppConfig`` — its ``projects`` are name → ``ProjectConfig(path=…)``.

    The path is not decoration: a configured project's ``.mcp.json`` is merged
    into the registry a session bound to it builds, so the path IS the cwd the
    tools catalog has to probe.
    """
    return SimpleNamespace(
        projects={name: SimpleNamespace(path=path) for name, path in projects},
        llm=llm or _FakeLlm(),
    )


def _managed(name, path, *, is_worktree=False):
    """One ``VirtualProject`` row of the managed-project store."""
    return SimpleNamespace(name=name, path=path, is_worktree=is_worktree)


def _fake_fan_out():
    """Two capability-gated plugins + one that gates nothing (it still ships tools)."""
    return SimpleNamespace(
        mcp_servers={},
        session_tool_entries=[{"tool_id": "wiki_search"}, {"tool_id": "scg_map"}],
        components=[
            SimpleNamespace(
                manifest=SimpleNamespace(name="wiki", requires_capabilities=("wiki",)),
                agent_files=[],
            ),
            SimpleNamespace(
                manifest=SimpleNamespace(name="widget-builder", requires_capabilities=("stlite",)),
                agent_files=[],
            ),
        ],
    )


class _FakeRegistryLoader:
    """``get_or_build_registry`` — and it RECORDS every cwd it is asked for.

    The cwd is the whole point of this fake. The registry a session builds merges
    ``<cwd>/.mcp.json``, so a catalog that only ever loads at ``cwd=None`` omits
    MCP tool ids that real sessions hold. The previous stub was a
    ``lambda **_kwargs``, which SWALLOWED ``cwd`` — a probe asking only for
    ``None`` looked identical to one asking for every project, and the invariant
    was owned by no test on either side. ``per_cwd`` maps a cwd to the tool ids
    visible ONLY there (a project's own ``.mcp.json``).
    """

    def __init__(self, *shared, per_cwd=None):
        self.shared = shared
        self.per_cwd = per_cwd or {}
        self.cwds = []

    def __call__(self, *, cwd=None, extra_mcp_servers=None):
        self.cwds.append(cwd)
        specs = [
            SimpleNamespace(tool_id=tool_id)
            for tool_id in (*self.shared, *self.per_cwd.get(cwd, ()))
        ]
        return SimpleNamespace(list_specs=lambda include_disabled=False: specs)


def _fake_registry(*tool_ids, per_cwd=None):
    return _FakeRegistryLoader(*tool_ids, per_cwd=per_cwd)


def _fake_store(*projects):
    projects = projects or (_managed("managed-one", "/srv/managed-one"),)
    return SimpleNamespace(list_projects=lambda: list(projects))


def _sources(**overrides):
    """A real ``InstructionValueSources`` whose I/O boundaries are stubbed."""
    kwargs = {
        "project_store": _fake_store(),
        "config_provider": _fake_config,
        "registry_loader": _fake_registry("shell", "file_edit"),
        "plugin_loader": _fake_fan_out,
        "agent_parser": lambda path, source: None,
    }
    kwargs.update(overrides)
    return InstructionValueSources(**kwargs)


# ---------------------------------------------------------------------------
# GET — singleton, never 404s
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_returns_defaults_when_unset(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.get(PATH, headers=auth_headers)
        assert resp.status_code == 200
        dto = resp.get_json()
        assert dto["id"] == GLOBAL_INSTRUCTIONS_ID
        assert dto["template"] == ""
        assert dto["enabled"] is True
        assert dto["lastError"] is None

    def test_get_wire_is_camel_case_and_owned_by_the_app(self, client, auth_headers, tmp_path):
        """The response is the APP's DTO, not core's persistence model.

        Asserting the EXACT key set (not just "updatedAt is present") is the
        point: this is what fails the day core adds a field to
        ``SystemInstructionsDoc``'s storage shape, instead of that field
        silently auto-leaking to every console client.
        """
        _setup(tmp_path)
        dto = client.get(PATH, headers=auth_headers).get_json()
        assert set(dto) == {"id", "template", "enabled", "updatedAt", "lastError"}

    def test_get_requires_auth(self, client, tmp_path):
        _setup(tmp_path)
        assert client.get(PATH).status_code == 401


# ---------------------------------------------------------------------------
# PUT — full upsert
# ---------------------------------------------------------------------------


class TestPut:
    def test_put_roundtrips_and_is_read_back(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.put(
            PATH, headers=auth_headers, json={"template": "Hello {{ surface }}.", "enabled": False}
        )
        assert resp.status_code == 200
        dto = resp.get_json()
        assert dto["template"] == "Hello {{ surface }}."
        assert dto["enabled"] is False
        assert dto["lastError"] is None
        assert dto["updatedAt"]  # server-stamped

        again = client.get(PATH, headers=auth_headers).get_json()
        assert again["template"] == "Hello {{ surface }}."
        assert again["enabled"] is False

    def test_put_defaults_enabled_true(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.put(PATH, headers=auth_headers, json={"template": "hi"})
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is True

    def test_put_clears_last_error_on_success(self, client, auth_headers, tmp_path):
        store = _setup(tmp_path)
        # Seed a doc carrying a stale render error (as if a prior run failed).
        from mewbo_core.system_instructions import SystemInstructionsDoc

        store.put(SystemInstructionsDoc(template="{{ broken", last_error="some prior failure"))
        resp = client.put(PATH, headers=auth_headers, json={"template": "fine now"})
        assert resp.status_code == 200
        assert resp.get_json()["lastError"] is None

    def test_put_rejects_uncompilable_template_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.put(PATH, headers=auth_headers, json={"template": "{% if surface %}no endif"})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"]["code"] == "validation"
        assert "syntax" in body["error"]["reason"].lower()
        # Nothing was persisted — a broken template never reaches the store.
        assert client.get(PATH, headers=auth_headers).get_json()["template"] == ""

    def test_put_rejects_server_owned_field_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.put(
            PATH,
            headers=auth_headers,
            json={"template": "hi", "updated_at": "2020-01-01T00:00:00+00:00"},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "validation"

    def test_put_rejects_id_field_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.put(PATH, headers=auth_headers, json={"template": "hi", "id": "not-global"})
        assert resp.status_code == 400

    def test_put_rejects_last_error_field_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.put(
            PATH, headers=auth_headers, json={"template": "hi", "last_error": "smuggled"}
        )
        assert resp.status_code == 400

    def test_put_rejects_missing_template_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.put(PATH, headers=auth_headers, json={"enabled": True})
        assert resp.status_code == 400

    def test_put_rejects_oversized_template_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        # Comfortably over MAX_TEMPLATE_BYTES (64KiB); plain text so it still compiles.
        huge = "x" * (70 * 1024)
        resp = client.put(PATH, headers=auth_headers, json={"template": huge})
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "validation"

    def test_put_requires_auth(self, client, tmp_path):
        _setup(tmp_path)
        assert client.put(PATH, json={"template": "hi"}).status_code == 401


# ---------------------------------------------------------------------------
# POST /preview — never 500s, surfaces context per surface
# ---------------------------------------------------------------------------


class TestPreview:
    SURFACE_TEMPLATE = "{% if surface == 'android' %}MOBILE{% else %}DESKTOP{% endif %}"

    def test_preview_differs_by_surface_override(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        android = client.post(
            f"{PATH}/preview",
            headers=auth_headers,
            json={"template": self.SURFACE_TEMPLATE, "context": {"surface": "android"}},
        )
        cli = client.post(
            f"{PATH}/preview",
            headers=auth_headers,
            json={"template": self.SURFACE_TEMPLATE, "context": {"surface": "cli"}},
        )
        assert android.status_code == cli.status_code == 200
        assert android.get_json() == {"rendered": "MOBILE", "error": None}
        assert cli.get_json() == {"rendered": "DESKTOP", "error": None}

    def test_preview_default_context_has_no_surface_override(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.post(
            f"{PATH}/preview", headers=auth_headers, json={"template": self.SURFACE_TEMPLATE}
        )
        assert resp.status_code == 200
        # No override ⇒ the curated sample surface ("cli") is used, not android.
        assert resp.get_json()["rendered"] == "DESKTOP"

    def test_preview_broken_template_returns_200_with_error(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.post(
            f"{PATH}/preview", headers=auth_headers, json={"template": "{% if surface %}no endif"}
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["rendered"] == ""
        assert body["error"] is not None
        assert "syntax" in body["error"].lower()

    def test_preview_undefined_variable_is_not_an_error(self, client, auth_headers, tmp_path):
        # ChainableUndefined: an unknown/future variable renders blank, never raises.
        _setup(tmp_path)
        resp = client.post(
            f"{PATH}/preview",
            headers=auth_headers,
            json={"template": "[{{ some_future_field }}]"},
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"rendered": "[]", "error": None}

    def test_preview_omitted_template_uses_stored(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        client.put(PATH, headers=auth_headers, json={"template": "stored: {{ surface }}"})
        resp = client.post(f"{PATH}/preview", headers=auth_headers, json={})
        assert resp.status_code == 200
        assert resp.get_json()["rendered"].startswith("stored:")

    def test_preview_bad_context_override_400(self, client, auth_headers, tmp_path):
        _setup(tmp_path)
        resp = client.post(
            f"{PATH}/preview",
            headers=auth_headers,
            json={"template": "hi", "context": {"not_a_real_field": "x"}},
        )
        assert resp.status_code == 400

    def test_preview_requires_auth(self, client, tmp_path):
        _setup(tmp_path)
        assert client.post(f"{PATH}/preview", json={"template": "hi"}).status_code == 401


# ---------------------------------------------------------------------------
# GET /variables — generated from InstructionContext, never hand-authored
# ---------------------------------------------------------------------------


class TestVariables:
    def _rows(self, client, auth_headers, tmp_path, **overrides):
        _setup(tmp_path, value_sources=_sources(**overrides))
        resp = client.get(f"{PATH}/variables", headers=auth_headers)
        assert resp.status_code == 200
        return {row["name"]: row for row in resp.get_json()["variables"]}

    def test_variables_match_instruction_context_schema(self, client, auth_headers, tmp_path):
        _setup(tmp_path, value_sources=_sources())
        resp = client.get(f"{PATH}/variables", headers=auth_headers)
        assert resp.status_code == 200
        rows = resp.get_json()["variables"]

        schema_props = InstructionContext.model_json_schema()["properties"]
        assert {row["name"] for row in rows} == set(schema_props)
        assert len(rows) == len(InstructionContext.model_fields)

        by_name = {row["name"]: row for row in rows}
        # Every documented field carries a non-empty description (the contract
        # requires `Field(description=...)` on every InstructionContext field) —
        # and the row's description matches the schema's verbatim (derived, not
        # a hand-copied duplicate that could drift).
        for name, prop in schema_props.items():
            assert by_name[name]["description"] == prop.get("description", "")
            assert by_name[name]["description"]

        assert by_name["is_mobile"]["type"] == "boolean"
        assert by_name["capabilities"]["type"] == "array"
        # `project: str | None` — an Optional field resolves to its non-null arm.
        assert by_name["project"]["type"] == "string"

    def test_row_wire_shape_is_exactly_the_contract(self, client, auth_headers, tmp_path):
        """The EXACT key set — and no ``enum``.

        ``enum`` was dropped when ``values``/``valuesKind`` landed: the pair says
        the same thing strictly better, and a second channel for one fact is the
        drift this asserts against. The console is coded to these six keys.
        """
        by_name = self._rows(client, auth_headers, tmp_path)
        for row in by_name.values():
            assert set(row) == {
                "name",
                "type",
                "description",
                "values",
                "valuesKind",
                "valuesNote",
            }

    def test_origin_is_closed_with_the_real_session_origin_members(
        self, client, auth_headers, tmp_path
    ):
        """``origin`` is the ONE closed list: a session's value is always a member.

        Values are derived FROM the enum, never hardcoded, so adding a
        ``SessionOrigin`` member cannot leave this asserting a stale set. The
        ``closed`` kind is what tells an operator an ``else`` branch is
        genuinely unreachable — everything else on this page is merely what
        happens to be installed.
        """
        by_name = self._rows(client, auth_headers, tmp_path)

        assert by_name["origin"]["type"] == "string"
        assert by_name["origin"]["values"] == [member.value for member in SessionOrigin]
        assert by_name["origin"]["valuesKind"] == "closed"
        assert by_name["origin"]["valuesNote"]

    def test_live_sources_land_on_their_variables_as_known(self, client, auth_headers, tmp_path):
        """Each deployment-sourced variable carries its live values, marked ``known``."""
        by_name = self._rows(client, auth_headers, tmp_path)

        # tools = registry specs UNION the plugin session tools — a superset of
        # what any single session reports, which is the whole contract.
        assert by_name["tools"]["values"] == ["file_edit", "scg_map", "shell", "wiki_search"]
        assert by_name["tools"]["valuesKind"] == "known"
        assert by_name["tools"]["valuesNote"]

        # capabilities: the union of the installed plugins' requires-capabilities.
        assert by_name["capabilities"]["values"] == ["stlite", "wiki"]
        assert by_name["capabilities"]["valuesKind"] == "known"

        # projects: config-declared UNION managed.
        assert by_name["project"]["values"] == ["managed-one", "mewbo"]
        assert by_name["model"]["values"] == ["gpt-5.4", "claude-opus-4-8"]

    def test_variable_with_no_source_has_null_values(self, client, auth_headers, tmp_path):
        """A field nothing can source values for goes out with all three keys null."""
        by_name = self._rows(client, auth_headers, tmp_path)

        for name in ("session_id", "hostname", "cwd", "is_mobile", "mewbo_version"):
            assert by_name[name]["values"] is None
            assert by_name[name]["valuesKind"] is None
            assert by_name[name]["valuesNote"] is None

    def test_dead_llm_proxy_does_not_take_the_page_down(self, client, auth_headers, tmp_path):
        """THE test: one dead source empties ITS row, and nothing else.

        This endpoint is what an operator opens to FIX a broken template — often
        precisely because their deployment is misbehaving. ``list_models`` makes
        a live HTTP call to the LiteLLM proxy and raises ``ValueError`` when it
        is down, so a 500 here would mean the debugging surface dies exactly
        when it is needed. ``model`` comes back with null values (core renders
        an empty source as "we could not ask", never as an empty list); every
        other row is intact.
        """
        dead_proxy = _fake_config(llm=_FakeLlm(error=ValueError("Model listing failed: timed out")))
        by_name = self._rows(client, auth_headers, tmp_path, config_provider=lambda: dead_proxy)

        assert by_name["model"]["values"] is None
        assert by_name["model"]["valuesKind"] is None
        # The other probes are untouched — including `project`, which reads the
        # SAME config object whose llm block just blew up.
        assert by_name["tools"]["values"] == ["file_edit", "scg_map", "shell", "wiki_search"]
        assert by_name["capabilities"]["values"] == ["stlite", "wiki"]
        assert by_name["project"]["values"] == ["managed-one", "mewbo"]
        assert by_name["origin"]["valuesKind"] == "closed"

    def test_dead_plugin_discovery_does_not_take_the_page_down(
        self, client, auth_headers, tmp_path
    ):
        """The mirror case: a source feeding TWO probes dies, the rest still render."""

        def boom():
            raise RuntimeError("MCP discovery exploded")

        by_name = self._rows(client, auth_headers, tmp_path, plugin_loader=boom)

        assert by_name["tools"]["values"] is None
        assert by_name["capabilities"]["values"] is None
        assert by_name["model"]["values"] == ["gpt-5.4", "claude-opus-4-8"]
        assert by_name["project"]["values"] == ["managed-one", "mewbo"]

    def test_variables_requires_auth(self, client, tmp_path):
        _setup(tmp_path)
        assert client.get(f"{PATH}/variables").status_code == 401


# ---------------------------------------------------------------------------
# InstructionValueSources — the I/O edge, straight (no HTTP in the way)
# ---------------------------------------------------------------------------


class TestValueSources:
    def test_a_hanging_source_is_bounded_by_the_deadline(self):
        """A probe that never returns must not hang the request — try/except can't do this.

        The bound is what separates "a dead proxy" (an exception) from "a
        hanging MCP server" (no exception, ever). The catalog settles at the
        deadline with that one source empty.
        """

        def never_returns():
            time.sleep(30)
            raise AssertionError("unreachable")

        sources = _sources(plugin_loader=never_returns)
        sources.probe_timeout = 0.05

        started = time.monotonic()
        catalog = sources.catalog()
        elapsed = time.monotonic() - started

        assert elapsed < 5.0  # the 30s sleep is abandoned, not awaited
        assert catalog.tools == ()
        assert catalog.capabilities == ()
        # The probes that DID answer are all present.
        assert catalog.models == ("gpt-5.4", "claude-opus-4-8")
        assert catalog.projects == ("managed-one", "mewbo")

    def test_catalog_leaves_the_static_vocabularies_to_core(self):
        """``surfaces``/``platforms`` are core's own knowledge — the edge never re-derives them."""
        catalog = _sources().catalog()

        assert catalog.surfaces == type(catalog).model_fields["surfaces"].default
        assert catalog.platforms == type(catalog).model_fields["platforms"].default


# ---------------------------------------------------------------------------
# The superset invariant — the catalog must cover every CWD a session can hold
# ---------------------------------------------------------------------------


class TestToolsCatalogIsASuperset:
    """``tools`` must be a superset of what ANY session reports, across every scope.

    The registry a session builds merges its project's ``.mcp.json`` (and the
    subtree's), so a catalog loaded only at ``cwd=None`` does not merely report a
    WIDER list than one session's: it reports a DIFFERENT one, missing ids that
    the operator's own sessions genuinely hold. That is the "the reference lies"
    failure the whole feature exists to prevent, so the cwd set is pinned here.
    """

    def _sources_over(self, loader, *, projects, store=None):
        return _sources(
            registry_loader=loader,
            config_provider=lambda: _fake_config(projects=projects),
            project_store=store or _fake_store(),
        )

    def test_catalog_probes_none_plus_every_configured_project(self, tmp_path):
        """``None`` AND each CONFIGURED project path — and deliberately nothing else.

        Managed projects are NOT probed, and asserting the exact cwd set (rather
        than mere membership) is what keeps that a decision rather than an
        accident. They are server-created, so the set is unbounded and churning:
        probing it would put an O(store-size) fan of live MCP discovery on the
        settings page, blow the shared deadline, and empty the very row it was
        meant to complete. A dev box's store held 974 of them, nearly all dead
        ``/tmp`` paths from old test runs.
        """
        mewbo = tmp_path / "mewbo"
        service = tmp_path / "my-service"
        managed = tmp_path / "managed-one"
        for path in (mewbo, service, managed):
            path.mkdir()

        loader = _fake_registry("shell")
        sources = self._sources_over(
            loader,
            projects=(("mewbo", str(mewbo)), ("my-service", str(service))),
            store=_fake_store(
                _managed("managed-one", str(managed)),
                _managed("issue-7", str(tmp_path / "issue-7"), is_worktree=True),
            ),
        )

        sources.catalog()

        assert set(loader.cwds) == {None, str(mewbo), str(service)}
        # Each scope is loaded exactly once — the registry cache is keyed by cwd,
        # and a repeated load would churn the shared MCP pool for nothing.
        assert len(loader.cwds) == len(set(loader.cwds))

    def test_a_stale_config_path_is_not_probed(self, tmp_path):
        """A configured project whose directory is gone never spends the deadline.

        ``app.json`` outlives the filesystem; building a registry for a directory
        that is not there buys nothing and costs the one budget the tools row has.
        """
        live = tmp_path / "live"
        live.mkdir()
        loader = _fake_registry("shell")

        self._sources_over(
            loader,
            projects=(("live", str(live)), ("deleted", str(tmp_path / "gone"))),
        ).catalog()

        assert set(loader.cwds) == {None, str(live)}

    def test_a_project_only_mcp_tool_id_reaches_the_catalog(self, tmp_path):
        """A tool id that exists ONLY at a project's cwd is in the catalog.

        The project's ``.mcp.json`` declares a server, so a session scoped there
        holds ``mcp__my_service__deploy`` in ``InstructionContext.tools``. Loaded
        only at ``cwd=None``, the catalog did not merely report a WIDER list than
        that session's, it reported a DIFFERENT one — omitting an id the
        operator's own sessions genuinely hold, so a
        ``{% if 'mcp__my_service__deploy' in tools %}`` branch looked impossible
        to write against a tool they were actually bound to. That is the "the
        reference lies" failure this whole feature exists to prevent.
        """
        service = tmp_path / "my-service"
        service.mkdir()
        loader = _fake_registry("shell", per_cwd={str(service): ("mcp__my_service__deploy",)})

        catalog = self._sources_over(loader, projects=(("my-service", str(service)),)).catalog()

        assert "mcp__my_service__deploy" in catalog.tools
        # The deployment-wide ids and the plugin session tools are still there:
        # the project scope is a UNION, never a replacement.
        assert catalog.tools == ("mcp__my_service__deploy", "scg_map", "shell", "wiki_search")
