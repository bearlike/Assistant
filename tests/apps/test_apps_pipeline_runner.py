"""Contract tests for the Phase 2 code-pipeline execution engine.

Covers the four surfaces this wave adds, all with the clock injected (``now=`` /
a fixed ``clock``) and real JSON stores (the data-plane + ledger math IS the
contract; only the failure handler / session backend are faked):

* **Model contracts** — ``PipelineSpec`` code-mode validators, ``PipelineRun``'s
  additive ``kind``/``params_hash``/``cache``, and the frozen ``PipelineResult``.
* **``AppPipelineRunner``** — happy path, params validation, TTL cache hit/miss,
  dry_run counts-without-writes, the workspace traversal guard, the wall-clock
  watchdog, non-JSON / oversized output rejection, the docs cap through ``ctx``,
  the banned-import lint gate, and the id-keyed ``run_pipeline`` adapter shape.
* **``AppPipelineRunTracker`` code-fire seam** — a fired ``mode="code"`` pipeline
  runs the ENGINE and writes a ``{kind:"scheduled"}`` ledger row with NO session
  re-engagement; a failed run closes ``failed`` + dispatches the policy; an
  agentic fire is unchanged (returns ``False`` → the caller re-engages).
* **``AppLifecycle`` submit** — a ``mode="code"`` pipeline whose entrypoint is not
  a bundle file is refused at submit (an actionable reask).
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest
from mewbo_api.apps.models import (
    AppFrontend,
    AppPolicies,
    AppSpec,
    CollectionSpec,
    PipelineIssue,
    PipelineResult,
    PipelineRun,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.pipeline_runner import (
    PIPELINE_ALLOWED_MODULES,
    AppPipelineRunner,
    PipelineExecutionError,
    PipelineExecutor,
    lint_pipeline,
)
from mewbo_api.apps.pipeline_tracker import AppPipelineRunTracker
from mewbo_api.apps.store import JsonAppDataStore, JsonAppStore, JsonPipelineRunStore
from mewbo_core.secret_redaction import get_secret_redactor
from pydantic import ValidationError

NOW = datetime(2026, 7, 18, 9, 0, 0, tzinfo=timezone.utc)
_OPEN_SCHEMA: dict[str, object] = {"type": "object"}

# A tiny real pipeline: glob two files, upsert one doc per file, return a summary.
_GLOB_PIPELINE = (
    "def run(params, ctx):\n"
    "    written = 0\n"
    "    for path in ctx.glob('*.md'):\n"
    "        body = ctx.read_file(path)\n"
    "        ctx.collection('notes').upsert(path, {'path': path, 'len': len(body)})\n"
    "        written += 1\n"
    "    return {'written': written}\n"
)
# Small named pipeline sources (kept out of inline args for readability + line length).
_RETURN_ONE = "def run(params, ctx):\n    return 1\n"
_ECHO_N = "def run(params, ctx):\n    return {'echo': params.get('n', 0)}\n"
_DOUBLE_N = "def run(params, ctx):\n    return params['n'] * 2\n"
_BAD_SCHEMA_WRITE = (
    "def run(params, ctx):\n    ctx.collection('notes').upsert('k', {'v': 'bad'})\n    return 1\n"
)
_READ_PARENT = "def run(params, ctx):\n    return ctx.read_file('../a.md')\n"
_READ_ABS = "def run(params, ctx):\n    return ctx.read_file('/etc/hostname')\n"
_SPIN = "import time\ndef run(params, ctx):\n    while True:\n        time.sleep(0.01)\n"
_IMPORT_OS = "import os\ndef run(params, ctx):\n    return os.getcwd()\n"
_DYN_EXEC = "def run(params, ctx):\n    return __import__('os').getcwd()\n"
_HASHLIB_OK = "import hashlib\ndef run(p, c):\n    return hashlib.sha256(b'x').hexdigest()\n"


def _code_pipeline(
    *,
    name: str = "p",
    entrypoint: str = "pipelines/p.py",
    trigger_ref: str | None = None,
    on_demand: bool = True,
    params_schema: dict[str, object] | None = None,
    cache_ttl_seconds: int = 0,
    cache_mode: str = "ttl",
    llm_budget_tokens: int = 0,
    timeout_seconds: int = 10,
    allow_exec: list[str] | None = None,
    allow_egress: list[str] | None = None,
) -> PipelineSpec:
    return PipelineSpec(
        name=name,
        wake_prompt="wake",
        mode="code",
        entrypoint=entrypoint,
        trigger_ref=trigger_ref,
        on_demand=on_demand if trigger_ref is None else False,
        params_schema=params_schema,
        cache_ttl_seconds=cache_ttl_seconds,
        cache_mode=cache_mode,
        llm_budget_tokens=llm_budget_tokens,
        timeout_seconds=timeout_seconds,
        allow_exec=allow_exec or [],
        allow_egress=allow_egress or [],
    )


def _app(
    *,
    pipeline: PipelineSpec,
    source: str = _GLOB_PIPELINE,
    entrypoint_key: str = "pipelines/p.py",
    collection_schema: dict[str, object] | None = None,
    policies: AppPolicies | None = None,
    maintainer: str | None = "maint-1",
) -> AppSpec:
    return AppSpec(
        app_id="app-x",
        title="App",
        owner_session_id="owner",
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(
            files={"app.py": "import streamlit as st", entrypoint_key: source}
        ),
        collections=[CollectionSpec(name="notes", json_schema=collection_schema or _OPEN_SCHEMA)],
        pipelines=[pipeline],
        policies=policies or AppPolicies(),
        maintainer_session_id=maintainer,
        status="live",
    )


@pytest.fixture
def workspace(tmp_path):
    """A two-file workspace directory the runner's ctx.glob/read_file see."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_text("alpha", encoding="utf-8")
    (ws / "b.md").write_text("bravo!", encoding="utf-8")
    return ws


def _runner(tmp_path, *, workspace=None, timeout_seconds=None, clock=lambda: NOW, llm_invoke=None):
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
    runner = AppPipelineRunner(
        app_store=app_store,
        app_data=data_store,
        workspace_resolver=lambda _app: str(workspace) if workspace is not None else None,
        clock=clock,
        timeout_seconds=timeout_seconds,
        llm_invoke=llm_invoke,
    )
    return runner, app_store, data_store


class _SpyLLM:
    """A fake ``llm_invoke`` — records every call and returns scripted responses.

    ``responses`` is either a single value returned for every call, or a list
    consumed one-per-call (a shorter list re-uses its last element). Records
    ``(prompt, schema, max_tokens)`` per call so a test can assert the retry seam
    fired (or that a cache hit skipped it entirely).
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, dict, int]] = []

    def __call__(self, prompt, schema, max_tokens):
        self.calls.append((prompt, schema, max_tokens))
        if isinstance(self.responses, list):
            idx = min(len(self.calls) - 1, len(self.responses) - 1)
            return self.responses[idx]
        return self.responses


# A schema requiring a single string ``title`` — used by the llm-step tests.
_TITLE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
_LLM_ONE = (
    "def run(params, ctx):\n"
    "    return ctx.llm('summarize the inbox', "
    "{'type': 'object', 'properties': {'title': {'type': 'string'}}, "
    "'required': ['title'], 'additionalProperties': False}, max_tokens=100)\n"
)
_LLM_TWICE = (
    "def run(params, ctx):\n"
    "    a = ctx.llm('first', {'type': 'object'}, max_tokens=100)\n"
    "    b = ctx.llm('second', {'type': 'object'}, max_tokens=100)\n"
    "    return [a, b]\n"
)


# ---------------------------------------------------------------------------
# Model contracts
# ---------------------------------------------------------------------------


class TestModels:
    def test_code_mode_requires_entrypoint(self):
        with pytest.raises(ValidationError, match="no `entrypoint`"):
            PipelineSpec(name="p", wake_prompt="w", on_demand=True, mode="code")

    def test_agentic_mode_rejects_entrypoint(self):
        with pytest.raises(ValidationError, match="applies only to mode='code'"):
            PipelineSpec(
                name="p", wake_prompt="w", on_demand=True, mode="agentic",
                entrypoint="pipelines/p.py",
            )

    def test_entrypoint_traversal_rejected_at_definition(self):
        with pytest.raises(ValidationError):
            PipelineSpec(
                name="p", wake_prompt="w", on_demand=True, mode="code",
                entrypoint="../escape.py",
            )

    def test_malformed_params_schema_rejected_at_definition(self):
        with pytest.raises(ValidationError, match="not a valid JSON Schema"):
            PipelineSpec(
                name="p", wake_prompt="w", on_demand=True, mode="code",
                entrypoint="pipelines/p.py", params_schema={"type": "not-a-real-type"},
            )

    def test_agentic_default_is_backwards_compatible(self):
        p = PipelineSpec(name="p", wake_prompt="w", on_demand=True)
        assert p.mode == "agentic"
        assert p.entrypoint is None
        assert p.cache_ttl_seconds == 0

    def test_wave5_field_defaults(self):
        p = PipelineSpec(name="p", wake_prompt="w", on_demand=True)
        assert p.cache_mode == "ttl"
        assert p.llm_budget_tokens == 0
        assert p.user_writable is False
        assert p.timeout_seconds == 10

    def test_wave5_fields_reject_negative_budget(self):
        with pytest.raises(ValidationError):
            PipelineSpec(name="p", wake_prompt="w", on_demand=True, llm_budget_tokens=-1)

    def test_timeout_seconds_bounds(self):
        # Declared per-pipeline (ge=1, le=600): 0 and >600 fail at definition.
        with pytest.raises(ValidationError):
            PipelineSpec(name="p", wake_prompt="w", on_demand=True, timeout_seconds=0)
        with pytest.raises(ValidationError):
            PipelineSpec(name="p", wake_prompt="w", on_demand=True, timeout_seconds=601)
        assert (
            PipelineSpec(
                name="p", wake_prompt="w", on_demand=True, timeout_seconds=300
            ).timeout_seconds
            == 300
        )

    def test_user_writable_requires_code_mode(self):
        # HISTORY-SAFE new model invariant (w5 fix): a user_writable agentic
        # pipeline mints an unusable write token, so it's unrepresentable.
        with pytest.raises(ValidationError, match="only a code pipeline"):
            PipelineSpec(name="p", wake_prompt="w", on_demand=True, user_writable=True)
        # ...but user_writable on a code pipeline is fine.
        ok = PipelineSpec(
            name="p", wake_prompt="w", on_demand=True, mode="code",
            entrypoint="pipelines/p.py", user_writable=True,
        )
        assert ok.user_writable is True

    def test_legacy_pipeline_dict_parses_with_new_fields_defaulted(self):
        # A bare pre-Wave-5 stored snapshot (no cache_mode/llm_budget_tokens/
        # user_writable/timeout_seconds) MUST still parse — history re-crosses the
        # parse seam under the contract it was WRITTEN with; the new fields default,
        # never 500 (the user_writable⇒code validator admits the False default).
        legacy = {"name": "ingest", "wake_prompt": "go", "trigger_ref": "trig-legacy"}
        p = PipelineSpec.model_validate(legacy)
        assert p.cache_mode == "ttl"
        assert p.llm_budget_tokens == 0
        assert p.user_writable is False
        assert p.timeout_seconds == 10
        assert p.mode == "agentic"

    def test_pipeline_run_open_stamps_kind_and_params_hash(self):
        run = PipelineRun.open(
            run_key="r1", app_id="a", pipeline_name="p", now=NOW,
            kind="on_request", params_hash="deadbeef",
        )
        assert run.kind == "on_request"
        assert run.params_hash == "deadbeef"
        assert run.cache is None

    def test_pipeline_run_close_records_cache(self):
        run = PipelineRun.open(run_key="r1", app_id="a", pipeline_name="p", now=NOW)
        run.close(now=NOW, status="succeeded", cache="hit")
        assert run.cache == "hit"

    def test_agentic_open_defaults_kind_scheduled(self):
        run = PipelineRun.open(run_key="r1", app_id="a", pipeline_name="p", now=NOW)
        assert run.kind == "scheduled"
        assert run.params_hash is None

    def test_pipeline_result_is_frozen(self):
        result = PipelineResult(output={"x": 1}, evaluated_at=NOW, cache="miss")
        assert result.model_config["frozen"] is True
        with pytest.raises(ValidationError):
            result.cache = "hit"  # type: ignore[misc]

    # -- allow_exec / allow_egress -------------------------------------------

    def test_allow_exec_egress_default_closed(self):
        p = PipelineSpec(name="p", wake_prompt="w", on_demand=True)
        assert p.allow_exec == []
        assert p.allow_egress == []

    def test_allow_exec_accepts_vetted_binaries(self):
        p = PipelineSpec(
            name="p", wake_prompt="w", on_demand=True, mode="code",
            entrypoint="pipelines/p.py", allow_exec=["git", "tea", "gh"],
        )
        assert p.allow_exec == ["git", "tea", "gh"]

    def test_allow_exec_rejects_unvetted_binary(self):
        with pytest.raises(ValidationError, match="unvetted binaries"):
            PipelineSpec(
                name="p", wake_prompt="w", on_demand=True, mode="code",
                entrypoint="pipelines/p.py", allow_exec=["curl"],
            )

    def test_allow_egress_accepts_bare_hostname_and_lowercases(self):
        p = PipelineSpec(
            name="p", wake_prompt="w", on_demand=True, mode="code",
            entrypoint="pipelines/p.py", allow_egress=["Git.Example.Com"],
        )
        assert p.allow_egress == ["git.example.com"]

    @pytest.mark.parametrize(
        "bad_host",
        ["https://git.example.com", "git.example.com/path", "user@git.example.com", ""],
    )
    def test_allow_egress_rejects_non_bare_hostname(self, bad_host):
        with pytest.raises(ValidationError, match="bare hostname"):
            PipelineSpec(
                name="p", wake_prompt="w", on_demand=True, mode="code",
                entrypoint="pipelines/p.py", allow_egress=[bad_host],
            )

    def test_legacy_pipeline_dict_parses_allow_exec_egress_defaults(self):
        # A stored snapshot from before these fields existed has neither one —
        # both default empty, never 500 on a detail read of an old app version.
        legacy = {"name": "ingest", "wake_prompt": "go", "trigger_ref": "trig-legacy"}
        p = PipelineSpec.model_validate(legacy)
        assert p.allow_exec == []
        assert p.allow_egress == []

    def test_allow_exec_or_egress_on_agentic_mode_rejected_at_definition(self):
        # Only a code pipeline is ever handed a `ctx`, so an agentic pipeline
        # declaring either list is a grant that reads as capability while
        # granting nothing reachable.
        with pytest.raises(ValidationError, match="only a code pipeline"):
            PipelineSpec(
                name="p", wake_prompt="w", on_demand=True, mode="agentic",
                allow_exec=["git"],
            )
        with pytest.raises(ValidationError, match="only a code pipeline"):
            PipelineSpec(
                name="p", wake_prompt="w", on_demand=True, mode="agentic",
                allow_egress=["git.example.com"],
            )

    def test_agentic_pipeline_with_empty_exec_egress_lists_still_parses(self):
        # HISTORY-SAFE: both fields default empty, so an agentic pipeline
        # carrying the (unused) empty defaults must still parse.
        p = PipelineSpec(
            name="p", wake_prompt="w", on_demand=True, mode="agentic",
            allow_exec=[], allow_egress=[],
        )
        assert p.mode == "agentic"
        assert p.allow_exec == []
        assert p.allow_egress == []


# ---------------------------------------------------------------------------
# AppPipelineRunner.execute
# ---------------------------------------------------------------------------


class TestExecuteHappyPath:
    def test_globs_reads_and_upserts_one_doc_per_file(self, tmp_path, workspace):
        runner, _, data_store = _runner(tmp_path, workspace=workspace)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline)

        result = runner.execute(app, pipeline, {}, now=NOW)

        assert result.cache == "miss"
        assert result.evaluated_at == NOW
        assert result.output == {"written": 2}
        assert result.docs_written == {"notes": 2}
        rows = {d.key: d.doc for d in data_store.query("app-x", "notes")}
        assert rows == {"a.md": {"path": "a.md", "len": 5}, "b.md": {"path": "b.md", "len": 6}}

    def test_no_workspace_means_empty_glob(self, tmp_path):
        runner, _, _ = _runner(tmp_path, workspace=None)
        pipeline = _code_pipeline()
        result = runner.execute(_app(pipeline=pipeline), pipeline, {}, now=NOW)
        assert result.output == {"written": 0}
        assert result.docs_written == {}


# ---------------------------------------------------------------------------
# ctx.exec — controlled, allowlisted subprocess egress
# ---------------------------------------------------------------------------

_EXEC_ECHO = (
    "def run(params, ctx):\n"
    "    res = ctx.exec(['git', 'rev-parse', '--is-inside-work-tree'])\n"
    "    return {'rc': res['returncode'], 'out': res['stdout'].strip()}\n"
)
_EXEC_UNDECLARED_BINARY = "def run(params, ctx):\n    return ctx.exec(['git', 'status'])\n"
_EXEC_UNDECLARED_HOST = (
    "def run(params, ctx):\n"
    "    return ctx.exec(['git', 'ls-remote', 'https://evil.example.com/x.git'])\n"
)
_EXEC_SCP_HOST = (
    "def run(params, ctx):\n"
    "    return ctx.exec(['git', 'ls-remote', 'user@evil.example.com:x.git'])\n"
)
# git's scp-style remote is `[user@]host:path` — the user segment is OPTIONAL, so
# a bypass matching only on `@` would let a bare `host:path` reach any host with
# an empty `allow_egress`.
_EXEC_SCP_HOST_NO_USER = (
    "def run(params, ctx):\n"
    "    return ctx.exec(['git', 'ls-remote', 'evil.example.com:x.git'])\n"
)
_EXEC_HOSTLESS = "def run(params, ctx):\n    return ctx.exec(['git', 'status'])\n"
_EXEC_MISSING_BINARY = (
    "def run(params, ctx):\n    return ctx.exec(['tea', 'issues', 'list'])\n"
)
_EXEC_NOT_A_LIST = "def run(params, ctx):\n    return ctx.exec('git status')\n"


@pytest.fixture
def git_workspace(tmp_path):
    """A real, initialized git workspace — ``ctx.exec`` runs an ACTUAL subprocess."""
    ws = tmp_path / "git_ws"
    ws.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    (ws / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True, env=env)
    return ws


class TestCtxExec:
    def test_declared_binary_runs_and_returns_shape(self, tmp_path, git_workspace):
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        app = _app(pipeline=pipeline, source=_EXEC_ECHO)

        result = runner.execute(app, pipeline, {}, now=NOW)

        assert result.output == {"rc": 0, "out": "true"}

    def test_undeclared_binary_is_refused_before_spawning(self, tmp_path, git_workspace):
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline()  # allow_exec defaults empty
        app = _app(pipeline=pipeline, source=_EXEC_UNDECLARED_BINARY)

        with pytest.raises(PipelineExecutionError, match="did not declare 'git'"):
            runner.execute(app, pipeline, {}, now=NOW)

    def test_undeclared_url_host_is_refused(self, tmp_path, git_workspace):
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])  # no allow_egress
        app = _app(pipeline=pipeline, source=_EXEC_UNDECLARED_HOST)

        with pytest.raises(PipelineExecutionError, match="evil.example.com"):
            runner.execute(app, pipeline, {}, now=NOW)

    def test_undeclared_scp_style_host_is_refused(self, tmp_path, git_workspace):
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        app = _app(pipeline=pipeline, source=_EXEC_SCP_HOST)

        with pytest.raises(PipelineExecutionError, match="evil.example.com"):
            runner.execute(app, pipeline, {}, now=NOW)

    def test_undeclared_scp_style_host_without_user_is_refused(self, tmp_path, git_workspace):
        # Same bypass as above, minus the optional `user@` — this is the shape
        # that actually got past an `@`-membership check in the past.
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        app = _app(pipeline=pipeline, source=_EXEC_SCP_HOST_NO_USER)

        with pytest.raises(PipelineExecutionError, match="evil.example.com"):
            runner.execute(app, pipeline, {}, now=NOW)

    def test_declared_host_is_permitted(self, tmp_path, git_workspace):
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"], allow_egress=["evil.example.com"])
        app = _app(pipeline=pipeline, source=_EXEC_UNDECLARED_HOST)

        # Declared host is now permitted through the gate; git itself fails
        # (no such remote) but that is a non-zero exit, not a rejection.
        result = runner.execute(app, pipeline, {}, now=NOW)
        assert result.output["returncode"] != 0

    def test_hostless_invocation_needs_no_allow_egress(self, tmp_path, git_workspace):
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])  # allow_egress stays empty
        app = _app(pipeline=pipeline, source=_EXEC_HOSTLESS)

        result = runner.execute(app, pipeline, {}, now=NOW)
        assert result.output["returncode"] == 0

    def test_missing_binary_raises_exec_error(self, tmp_path, git_workspace, monkeypatch):
        # A PATH with no real entries, so a declared-but-absent binary surfaces
        # as a clean exec error irrespective of whether this host has `tea`
        # installed. NOT "" — an EMPTY PATH makes `os.get_exec_path` return
        # [""], which resolves a bare name relative to CWD, so this test would
        # execute any file named `tea` sitting in the fixture workspace.
        monkeypatch.setenv("PATH", "/nonexistent")
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["tea"])
        app = _app(pipeline=pipeline, source=_EXEC_MISSING_BINARY)

        with pytest.raises(PipelineExecutionError, match="not installed"):
            runner.execute(app, pipeline, {}, now=NOW)

    def test_non_list_argv_is_rejected(self, tmp_path, git_workspace):
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        app = _app(pipeline=pipeline, source=_EXEC_NOT_A_LIST)

        with pytest.raises(PipelineExecutionError, match="non-empty list"):
            runner.execute(app, pipeline, {}, now=NOW)

    def test_no_workspace_raises_clean_workspace_error(self, tmp_path):
        runner, _, _ = _runner(tmp_path, workspace=None)
        pipeline = _code_pipeline(allow_exec=["git"])
        app = _app(pipeline=pipeline, source=_EXEC_HOSTLESS)

        with pytest.raises(PipelineExecutionError, match="no workspace is bound"):
            runner.execute(app, pipeline, {}, now=NOW)

    def test_stderr_and_stdout_are_redacted(self, tmp_path, git_workspace):
        # A credential-shaped string echoed back by the CLI must never reach
        # the pipeline's return value verbatim.
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        source = (
            "def run(params, ctx):\n"
            "    ctx.exec(['git', 'rev-parse', '--verify', "
            "'ghp_deadbeefdeadbeefdeadbeefdeadbeefdead'])\n"
            "    return ctx.exec(['git', 'log', '-1', '--format=%an <%ae>'])\n"
        )
        app = _app(pipeline=pipeline, source=source)
        result = runner.execute(app, pipeline, {}, now=NOW)
        assert "ghp_" not in result.output["stdout"]
        assert "ghp_" not in result.output["stderr"]

    def test_exec_is_refused_under_dry_run_and_never_spawns(
        self, tmp_path, git_workspace, monkeypatch
    ):
        def _unreachable_popen(*args, **kwargs):
            raise AssertionError("ctx.exec must refuse before subprocess.Popen is ever called")

        monkeypatch.setattr(subprocess, "Popen", _unreachable_popen)
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        app = _app(pipeline=pipeline, source=_EXEC_ECHO)

        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW, dry_run=True)

        assert exc.value.code == "dry_run"

    def test_per_call_timeout_is_clamped_by_pipeline_ceiling(
        self, tmp_path, git_workspace, monkeypatch
    ):
        captured: dict[str, object] = {}

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = argv

            def communicate(self, timeout=None):
                captured["timeout"] = timeout
                return "", ""

            returncode = 0

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"], timeout_seconds=2)
        source = (
            "def run(params, ctx):\n"
            "    return ctx.exec(['git', 'status'], timeout_seconds=100)\n"
        )
        app = _app(pipeline=pipeline, source=source)

        runner.execute(app, pipeline, {}, now=NOW)

        # A per-call override can only TIGHTEN the pipeline's own declared
        # ceiling — 100 was requested, but 2 is what reached communicate().
        assert captured["timeout"] == 2

    def test_timeout_raises_and_redacts_a_credential_from_argv(
        self, tmp_path, git_workspace, monkeypatch
    ):
        class _FakeTimeoutPopen:
            def __init__(self, argv, **kwargs):
                self._argv = argv
                self.pid = 999999999  # never a real process

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd=self._argv, timeout=timeout)

            returncode = -9

        monkeypatch.setattr(subprocess, "Popen", _FakeTimeoutPopen)
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        secret = "ghp_deadbeefdeadbeefdeadbeefdeadbeefdead"
        pipeline = _code_pipeline(allow_exec=["git"], allow_egress=["evil.example.com"])
        source = (
            "def run(params, ctx):\n"
            f"    return ctx.exec(['git', 'ls-remote', 'https://{secret}@evil.example.com/x.git'])\n"
        )
        app = _app(pipeline=pipeline, source=source)

        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)

        assert exc.value.code == "timeout"
        assert secret not in str(exc.value)

    def test_git_argv_gets_credential_helper_disabled_non_git_does_not(
        self, tmp_path, git_workspace, monkeypatch
    ):
        captured: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                captured.append(list(argv))

            def communicate(self, timeout=None):
                return "", ""

            returncode = 0

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git", "tea"])
        source = (
            "def run(params, ctx):\n"
            "    ctx.exec(['git', 'status'])\n"
            "    ctx.exec(['tea', 'issues', 'list'])\n"
            "    return 1\n"
        )
        app = _app(pipeline=pipeline, source=source)

        runner.execute(app, pipeline, {}, now=NOW)

        git_argv, tea_argv = captured
        assert git_argv[:3] == ["git", "-c", "credential.helper="]
        assert tea_argv == ["tea", "issues", "list"]  # untouched — not git

    @pytest.mark.parametrize(
        "argv",
        [
            # `-c` reaches git's own config, which is a program-execution surface:
            # an alias beginning `!` is a shell command, and `core.pager` is spawned.
            ["git", "-c", "alias.pwn=!id", "pwn"],
            ["git", "-c", "core.pager=sh -c id", "log"],
            # Banning the flags alone is not enough — `git config` PERSISTS the same
            # alias, so a later plain `git pwn` would run it. The subcommand list is
            # what closes that, and this case is why it is not redundant.
            ["git", "config", "alias.pwn", "!id"],
            # `ext::` is a transport whose "remote" IS a command line.
            ["git", "fetch", "ext::sh -c id"],
            ["git", "--exec-path=/tmp/evil", "log"],
            ["git", "ls-remote", "--upload-pack=id", "https://git.example.com/x.git"],
            # Write-shaped: not needed by a sync, and the read-only list excludes it.
            ["git", "push", "origin", "main"],
        ],
    )
    def test_git_argv_that_can_run_another_program_is_refused(self, argv):
        # Declaring `git` must not amount to declaring a shell. These all execute
        # arbitrary commands through git's own options if left ungated.
        pipeline = _code_pipeline(allow_exec=["git"], allow_egress=["git.example.com"])

        with pytest.raises(ValueError):
            pipeline.check_exec_allowed(argv)

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "log", "--oneline", "-20"],
            ["git", "diff", "HEAD~1", "--stat"],
            ["git", "show", "HEAD:README.md"],  # a refspec, not the host "HEAD"
            ["git", "status", "--porcelain"],
            ["git", "rev-parse", "HEAD"],
        ],
    )
    def test_read_shaped_git_calls_still_pass(self, argv):
        # The gate must not cost the flows this capability exists for.
        pipeline = _code_pipeline(allow_exec=["git"])

        pipeline.check_exec_allowed(argv)

    def test_config_injected_remote_url_is_refused(self):
        # urlparse() finds NO scheme in `remote.z.url=https://…` (the candidate
        # `remote.z.url=https` contains "="), so a parse-based extractor yields no
        # host and ALLOWS the call. A skipped token fails OPEN — this is the guard.
        pipeline = _code_pipeline(allow_exec=["git"], allow_egress=["git.example.com"])

        with pytest.raises(ValueError, match="allow_egress|not permitted"):
            pipeline.check_exec_allowed(
                ["git", "-c", "remote.z.url=https://evil.example.com/r.git", "fetch", "z"]
            )

    def test_one_token_carrying_two_hosts_yields_both(self):
        # `url.<a>.insteadOf=<b>` rewrites b -> a, so BOTH must be gated; a
        # first-match extractor would see only one.
        pipeline = _code_pipeline(allow_exec=["git"])

        hosts = pipeline.hosts_in_argv(
            ["git", "fetch", "url.https://a.example.com/.insteadOf=https://b.example.com/"]
        )

        assert hosts == {"a.example.com", "b.example.com"}

    def test_nonzero_exit_is_returned_not_raised(self, tmp_path, git_workspace):
        # A pipeline decides what a failed git exit means; ctx.exec must never
        # turn a plain non-zero return code into an exception.
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        # A PERMITTED subcommand that still exits non-zero — `git not-a-subcommand`
        # no longer reaches a process at all now that git argv is shape-gated.
        source = (
            "def run(params, ctx):\n"
            "    return ctx.exec(['git', 'rev-parse', '--verify', 'no-such-ref'])\n"
        )
        app = _app(pipeline=pipeline, source=source)

        result = runner.execute(app, pipeline, {}, now=NOW)

        assert result.output["returncode"] != 0


_GIT_ALIAS_INJECTION = (
    "def run(params, ctx):\n"
    "    return ctx.exec(['git', '-c', 'alias.x=!sh', 'x'])\n"
)
_GIT_READ_SHAPED = (
    "def run(params, ctx):\n"
    "    a = ctx.exec(['git', 'log', '--oneline', '-20'])\n"
    "    b = ctx.exec(['git', 'diff'])\n"
    "    return {'log_rc': a['returncode'], 'diff_rc': b['returncode']}\n"
)


class TestGitArgvShapeGate:
    """The git shape gate, proven END-TO-END — the breadth of refused shapes is
    covered as a pure rule on the model in ``TestCtxExec`` above.

    Two altitudes, deliberately not five tests each: the parametrized model tests
    prove the RULE, and these prove it is actually WIRED into ``ctx.exec`` rather
    than merely callable on the spec. Duplicating the whole matrix here would be
    the same fact asserted twice, drifting independently.
    """

    def test_alias_injection_is_refused_through_the_real_runner(self, tmp_path, git_workspace):
        # `git -c alias.x='!sh' x` is a host-less, ordinary-looking git call that
        # the binary+egress gate alone cannot see as unsafe.
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        app = _app(pipeline=pipeline, source=_GIT_ALIAS_INJECTION)

        with pytest.raises(PipelineExecutionError, match="not permitted"):
            runner.execute(app, pipeline, {}, now=NOW)

    def test_read_shaped_subcommands_still_allowed(self, tmp_path, git_workspace):
        # The gate must not cost the flows the capability exists for.
        runner, _, _ = _runner(tmp_path, workspace=git_workspace)
        pipeline = _code_pipeline(allow_exec=["git"])
        app = _app(pipeline=pipeline, source=_GIT_READ_SHAPED)

        result = runner.execute(app, pipeline, {}, now=NOW)

        assert result.output == {"log_rc": 0, "diff_rc": 0}


class TestPipelineExecutorDirect:
    """Direct contract tests on ``PipelineExecutor`` for a cap ``ctx.exec`` exposes
    no dial for (the per-call output byte budget) — everything else about the
    surface is covered end-to-end through ``ctx.exec`` above."""

    def test_output_is_capped_on_real_bytes_not_characters(self, tmp_path, monkeypatch):
        # Multi-byte UTF-8 so a character-count slice and a byte-count slice would
        # disagree; only a real byte budget keeps every produced chunk in bounds.
        big = "é" * 100  # 2 bytes each in utf-8 -> 200 bytes, 100 characters

        class _FakePopen:
            def __init__(self, *args, **kwargs):
                pass

            def communicate(self, timeout=None):
                return big, ""

            returncode = 0

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        pipeline = _code_pipeline(allow_exec=["git"])
        executor = PipelineExecutor(
            pipeline=pipeline,
            workspace_root=tmp_path,
            redactor=get_secret_redactor(),
            max_output_bytes=10,
        )

        result = executor.run(["git", "status"])

        # A naive `text[:10]` would keep 10 CHARACTERS (20 bytes) — this is what
        # distinguishes a byte budget from a character slice.
        assert len(result["stdout"].encode("utf-8")) <= 10
        assert len(result["stdout"]) < 100


class TestParamsValidation:
    def _schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
            "additionalProperties": False,
        }

    def test_valid_params_reach_ctx(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline(params_schema=self._schema())
        app = _app(pipeline=pipeline, source=_DOUBLE_N)
        assert runner.execute(app, pipeline, {"n": 21}, now=NOW).output == 42

    def test_wrong_param_type_raises_params(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline(params_schema=self._schema())
        app = _app(pipeline=pipeline, source=_RETURN_ONE)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {"n": "not-an-int"}, now=NOW)
        assert exc.value.code == "params"

    def test_params_supplied_when_none_accepted_raises(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()  # no params_schema
        app = _app(pipeline=pipeline, source=_RETURN_ONE)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {"n": 1}, now=NOW)
        assert exc.value.code == "params"


class TestCache:
    def _counter_app(self):
        pipeline = _code_pipeline(
            params_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
            cache_ttl_seconds=100,
        )
        app = _app(pipeline=pipeline, source=_ECHO_N)
        return app, pipeline

    def test_hit_within_ttl_returns_same_output_and_evaluated_at(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        app, pipeline = self._counter_app()
        first = runner.execute(app, pipeline, {"n": 1}, now=NOW)
        second = runner.execute(app, pipeline, {"n": 1}, now=NOW + timedelta(seconds=50))
        assert first.cache == "miss"
        assert second.cache == "hit"
        assert second.output == first.output
        assert second.evaluated_at == first.evaluated_at  # original computation time
        assert second.docs_written == {}  # a hit writes nothing

    def test_different_params_miss(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        app, pipeline = self._counter_app()
        runner.execute(app, pipeline, {"n": 1}, now=NOW)
        assert runner.execute(app, pipeline, {"n": 2}, now=NOW).cache == "miss"

    def test_expired_ttl_misses(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        app, pipeline = self._counter_app()
        runner.execute(app, pipeline, {"n": 1}, now=NOW)
        expired = runner.execute(app, pipeline, {"n": 1}, now=NOW + timedelta(seconds=200))
        assert expired.cache == "miss"

    def test_zero_ttl_never_caches(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()  # cache_ttl_seconds=0
        app = _app(pipeline=pipeline, source=_RETURN_ONE)
        assert runner.execute(app, pipeline, {}, now=NOW).cache == "miss"
        assert runner.execute(app, pipeline, {}, now=NOW).cache == "miss"


class TestDryRun:
    def test_counts_without_writing(self, tmp_path, workspace):
        runner, _, data_store = _runner(tmp_path, workspace=workspace)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline)
        result = runner.execute(app, pipeline, {}, now=NOW, dry_run=True)
        assert result.docs_written == {"notes": 2}  # what it WOULD write
        assert data_store.query("app-x", "notes") == []  # nothing durable

    def test_dry_run_still_schema_validates(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()
        schema = {"type": "object", "properties": {"v": {"type": "integer"}}, "required": ["v"]}
        app = _app(
            pipeline=pipeline,
            source=_BAD_SCHEMA_WRITE,
            collection_schema=schema,
        )
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW, dry_run=True)
        assert exc.value.code == "schema"

    def test_dry_run_bypasses_cache(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline(cache_ttl_seconds=100)
        app = _app(pipeline=pipeline, source=_RETURN_ONE)
        runner.execute(app, pipeline, {}, now=NOW)  # warms the cache
        assert runner.execute(app, pipeline, {}, now=NOW, dry_run=True).cache == "miss"


class TestGuards:
    def test_traversal_rejected(self, tmp_path, workspace):
        runner, _, _ = _runner(tmp_path, workspace=workspace)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline, source=_READ_PARENT)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "traversal"

    def test_absolute_path_rejected(self, tmp_path, workspace):
        runner, _, _ = _runner(tmp_path, workspace=workspace)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline, source=_READ_ABS)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "traversal"

    def test_timeout_returns_control_on_a_spin_loop(self, tmp_path):
        runner, _, _ = _runner(tmp_path, timeout_seconds=0.2)
        pipeline = _code_pipeline()
        # Sleep-loop: never returns on its own, near-zero CPU so the lingering
        # daemon thread doesn't burden the suite (the watchdog honesty note).
        app = _app(
            pipeline=pipeline,
            source=_SPIN,
        )
        started = time.monotonic()
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "timeout"
        assert time.monotonic() - started < 5.0  # control returned promptly

    def test_declared_timeout_seconds_is_honored(self, tmp_path):
        # No runner override (timeout_seconds=None) → the pipeline's DECLARED ceiling
        # bounds the watchdog. A 1s declared limit stops the spin loop in ~1s and the
        # error names that declared value, not a global default.
        runner, _, _ = _runner(tmp_path)  # override None → use the pipeline's value
        pipeline = _code_pipeline(timeout_seconds=1)
        app = _app(pipeline=pipeline, source=_SPIN)
        started = time.monotonic()
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "timeout"
        assert "1s" in exc.value.message  # the declared ceiling, not a global default
        assert time.monotonic() - started < 5.0

    def test_non_json_output_rejected(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline, source="def run(params, ctx):\n    return {1, 2, 3}\n")
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "output"

    def test_oversized_output_rejected(self, tmp_path):
        runner, app_store, data_store = _runner(tmp_path)
        runner = AppPipelineRunner(
            app_store=app_store, app_data=data_store,
            workspace_resolver=lambda _a: None, clock=lambda: NOW, max_output_bytes=64,
        )
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline, source="def run(params, ctx):\n    return 'x' * 500\n")
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "output"

    def test_docs_cap_enforced_through_ctx(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()
        app = _app(
            pipeline=pipeline,
            policies=AppPolicies(max_docs_per_collection=1),
            source=(
                "def run(params, ctx):\n"
                "    ctx.collection('notes').upsert('k1', {'v': 1})\n"
                "    ctx.collection('notes').upsert('k2', {'v': 2})\n"
                "    return 'done'\n"
            ),
        )
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "cap"

    def test_banned_import_rejected_by_lint(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline, source=_IMPORT_OS)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "lint"

    def test_dynamic_exec_rejected_by_lint(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline, source=_DYN_EXEC)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "lint"

    def test_runtime_error_bucketed(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline, source="def run(params, ctx):\n    return 1 / 0\n")
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "runtime"

    def test_missing_run_function_rejected(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        pipeline = _code_pipeline()
        app = _app(pipeline=pipeline, source="x = 1\n")
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "entrypoint"


class TestPipelineLintTable:
    def test_allows_server_side_stdlib(self):
        assert "hashlib" in PIPELINE_ALLOWED_MODULES
        assert "json" in PIPELINE_ALLOWED_MODULES
        assert lint_pipeline(_HASHLIB_OK) == []

    def test_bans_network_and_filesystem(self):
        assert "requests" not in PIPELINE_ALLOWED_MODULES
        assert "os" not in PIPELINE_ALLOWED_MODULES
        assert lint_pipeline("import socket\ndef run(p, c):\n    return 1\n")


# ---------------------------------------------------------------------------
# ctx.llm — the bounded LLM step (Wave 5)
# ---------------------------------------------------------------------------


class TestLLMStep:
    def test_happy_path_returns_validated_dict(self, tmp_path):
        spy = _SpyLLM({"title": "Inbox digest"})
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=1000)
        app = _app(pipeline=pipeline, source=_LLM_ONE)
        result = runner.execute(app, pipeline, {}, now=NOW)
        assert result.output == {"title": "Inbox digest"}
        assert len(spy.calls) == 1
        assert spy.calls[0][2] == 100  # max_tokens forwarded to the adapter

    def test_schema_violation_retries_once_then_fails(self, tmp_path):
        # The model returns a schema-violating dict BOTH times → one retry, then raise.
        spy = _SpyLLM({"headline": "wrong key"})  # missing required 'title'
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=1000)
        app = _app(pipeline=pipeline, source=_LLM_ONE)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "llm"
        assert len(spy.calls) == 2  # first attempt + ONE retry
        # The retry prompt carried the validation error back to the model.
        assert "rejected" in spy.calls[1][0]

    def test_schema_violation_then_valid_recovers_on_retry(self, tmp_path):
        spy = _SpyLLM([{"headline": "bad"}, {"title": "good"}])  # bad, then good
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=1000)
        app = _app(pipeline=pipeline, source=_LLM_ONE)
        assert runner.execute(app, pipeline, {}, now=NOW).output == {"title": "good"}
        assert len(spy.calls) == 2

    def test_budget_refusal_at_cap(self, tmp_path):
        # Two calls of 100 tokens each; budget 150 admits the first, refuses the second.
        spy = _SpyLLM({"ok": 1})
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=150)
        app = _app(pipeline=pipeline, source=_LLM_TWICE)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "llm"
        assert "budget exceeded" in exc.value.message
        assert len(spy.calls) == 1  # only the first call reached the model

    def test_zero_budget_forbids_llm(self, tmp_path):
        spy = _SpyLLM({"title": "x"})
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=0)  # the default — llm forbidden
        app = _app(pipeline=pipeline, source=_LLM_ONE)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "llm"
        assert "llm_budget_tokens" in exc.value.message
        assert spy.calls == []  # never reached the model

    def test_unwired_runner_raises_clean_not_configured(self, tmp_path):
        runner, _, _ = _runner(tmp_path, llm_invoke=None)  # no model backend wired
        pipeline = _code_pipeline(llm_budget_tokens=1000)
        app = _app(pipeline=pipeline, source=_LLM_ONE)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "llm"
        assert "not configured" in exc.value.message

    def test_cache_hit_skips_the_model(self, tmp_path):
        # Same prompt+schema across two executes → the content-addressed llm cache
        # serves the second, so the model is invoked exactly once.
        spy = _SpyLLM({"title": "digest"})
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=1000)
        app = _app(pipeline=pipeline, source=_LLM_ONE)
        first = runner.execute(app, pipeline, {}, now=NOW)
        second = runner.execute(app, pipeline, {}, now=NOW + timedelta(seconds=10))
        assert first.output == second.output == {"title": "digest"}
        assert len(spy.calls) == 1  # the second execute hit the llm cache

    def test_llm_allowed_in_dry_run(self, tmp_path):
        spy = _SpyLLM({"title": "preview"})
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=1000)
        app = _app(pipeline=pipeline, source=_LLM_ONE)
        result = runner.execute(app, pipeline, {}, now=NOW, dry_run=True)
        assert result.output == {"title": "preview"}
        assert len(spy.calls) == 1  # the builder can test ctx.llm under dry_run

    def test_dry_run_reads_cache_but_never_writes_it(self, tmp_path):
        # A dry-run preview must NOT mint an llm cache entry a later real (charged)
        # run would then serve un-provenanced: dry_run FIRST, then a real run with
        # the SAME prompt still invokes the model (the dry run wrote no cache entry).
        spy = _SpyLLM({"title": "preview"})
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=1000)
        app = _app(pipeline=pipeline, source=_LLM_ONE)
        runner.execute(app, pipeline, {}, now=NOW, dry_run=True)  # preview: 1 call, no write
        runner.execute(app, pipeline, {}, now=NOW)  # real: cache empty → a 2nd call
        assert len(spy.calls) == 2

    def test_non_object_output_schema_rejected_before_model_call(self, tmp_path):
        # A non-object root schema is rejected at ctx.llm ENTRY — before any model
        # call (which would only waste a round-trip that fails re-validation).
        spy = _SpyLLM(["not", "a", "dict"])
        runner, _, _ = _runner(tmp_path, llm_invoke=spy)
        pipeline = _code_pipeline(llm_budget_tokens=1000)
        source = (
            "def run(params, ctx):\n"
            "    return ctx.llm('x', {'type': 'array', 'items': {'type': 'string'}})\n"
        )
        app = _app(pipeline=pipeline, source=source)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.execute(app, pipeline, {}, now=NOW)
        assert exc.value.code == "llm"
        assert "root type 'object'" in exc.value.message
        assert spy.calls == []  # never reached the model

    def test_source_change_busts_llm_cache_in_source_mode(self, tmp_path, workspace):
        # In cache_mode="source", the ctx.llm cache is salted with the run's source
        # fingerprint: a source change re-executes AND re-invokes the model — a
        # stale prompt-keyed llm answer must NOT be served on the re-execution.
        spy = _SpyLLM({"title": "digest"})
        runner, _, _ = _runner(tmp_path, workspace=workspace, llm_invoke=spy)
        source = (
            "def run(params, ctx):\n"
            "    for path in ctx.glob('*.md'):\n"
            "        ctx.read_file(path)\n"
            "    return ctx.llm('summarize', {'type': 'object', "
            "'properties': {'title': {'type': 'string'}}, 'required': ['title'], "
            "'additionalProperties': False})\n"
        )
        pipeline = _code_pipeline(cache_mode="source", llm_budget_tokens=2000)
        app = _app(pipeline=pipeline, source=source)
        first = runner.execute(app, pipeline, {}, now=NOW)
        assert first.cache == "miss"
        assert len(spy.calls) == 1
        # Change a globbed source file → source cache busts → re-execute → the llm
        # cache salt moves with it → the model is called AGAIN (not served stale).
        (workspace / "a.md").write_text("changed content", encoding="utf-8")
        second = runner.execute(app, pipeline, {}, now=NOW + timedelta(seconds=1))
        assert second.cache == "miss"
        assert len(spy.calls) == 2


# ---------------------------------------------------------------------------
# cache_mode="source" — read-through liveness (Wave 5)
# ---------------------------------------------------------------------------


class TestSourceCacheMode:
    def _source_app(self, cache_ttl_seconds=0):
        pipeline = _code_pipeline(cache_mode="source", cache_ttl_seconds=cache_ttl_seconds)
        return pipeline, _app(pipeline=pipeline)  # _GLOB_PIPELINE globs *.md + reads each

    def test_first_run_always_executes(self, tmp_path, workspace):
        runner, _, _ = _runner(tmp_path, workspace=workspace)
        pipeline, app = self._source_app()
        result = runner.execute(app, pipeline, {}, now=NOW)
        assert result.cache == "miss"  # no manifest yet
        assert result.output == {"written": 2}

    def test_hit_on_unchanged_stats(self, tmp_path, workspace):
        runner, _, data_store = _runner(tmp_path, workspace=workspace)
        pipeline, app = self._source_app()
        first = runner.execute(app, pipeline, {}, now=NOW)
        second = runner.execute(app, pipeline, {}, now=NOW + timedelta(hours=99))
        assert first.cache == "miss"
        assert second.cache == "hit"  # source unchanged → served from cache
        assert second.evaluated_at == first.evaluated_at  # original computation time
        assert second.docs_written == {}  # a hit re-writes nothing

    def test_bust_on_mtime_change(self, tmp_path, workspace):
        runner, _, _ = _runner(tmp_path, workspace=workspace)
        pipeline, app = self._source_app()
        runner.execute(app, pipeline, {}, now=NOW)
        # Bump ONLY a.md's mtime (content/size untouched) → the stat fingerprint moves.
        a_md = workspace / "a.md"
        st = a_md.stat()
        os.utime(a_md, ns=(st.st_mtime_ns + 10**9, st.st_mtime_ns + 10**9))
        assert runner.execute(app, pipeline, {}, now=NOW).cache == "miss"

    def test_bust_on_new_file_matching_a_recorded_glob(self, tmp_path, workspace):
        runner, _, _ = _runner(tmp_path, workspace=workspace)
        pipeline, app = self._source_app()
        runner.execute(app, pipeline, {}, now=NOW)  # globbed *.md → [a.md, b.md]
        (workspace / "c.md").write_text("charlie", encoding="utf-8")  # NEW glob match
        result = runner.execute(app, pipeline, {}, now=NOW)
        assert result.cache == "miss"  # the glob result set changed → cache busted
        assert result.output == {"written": 3}  # re-executed, saw c.md

    def test_unrelated_new_file_does_not_bust(self, tmp_path, workspace):
        runner, _, _ = _runner(tmp_path, workspace=workspace)
        pipeline, app = self._source_app()
        runner.execute(app, pipeline, {}, now=NOW)
        (workspace / "notes.txt").write_text("not markdown", encoding="utf-8")  # no *.md match
        assert runner.execute(app, pipeline, {}, now=NOW).cache == "hit"

    def test_source_mode_ignores_cache_ttl_seconds(self, tmp_path, workspace):
        # cache_ttl_seconds is set, but source mode is stat-driven: a change busts
        # even inside the "TTL window", and an unchanged source serves indefinitely.
        runner, _, _ = _runner(tmp_path, workspace=workspace)
        pipeline, app = self._source_app(cache_ttl_seconds=100000)
        runner.execute(app, pipeline, {}, now=NOW)
        # Well within the TTL window, but a source change still busts.
        (workspace / "c.md").write_text("charlie", encoding="utf-8")
        assert runner.execute(app, pipeline, {}, now=NOW + timedelta(seconds=1)).cache == "miss"

    def test_dry_run_never_serves_or_populates_source_cache(self, tmp_path, workspace):
        runner, _, data_store = _runner(tmp_path, workspace=workspace)
        pipeline, app = self._source_app()
        dry = runner.execute(app, pipeline, {}, now=NOW, dry_run=True)
        assert dry.cache == "miss"
        assert data_store.query("app-x", "notes") == []  # dry_run wrote nothing durable
        # A subsequent real run still executes (the dry run populated no source cache).
        assert runner.execute(app, pipeline, {}, now=NOW).cache == "miss"


# ---------------------------------------------------------------------------
# The id-keyed run_pipeline adapter (the run_pipeline tool's Protocol)
# ---------------------------------------------------------------------------


class TestRunPipelineAdapter:
    def test_resolves_by_id_and_returns_tool_dict(self, tmp_path, workspace):
        runner, app_store, _ = _runner(tmp_path, workspace=workspace)
        pipeline = _code_pipeline()
        app_store.save(_app(pipeline=pipeline))
        out = runner.run_pipeline("app-x", "p", params={}, dry_run=False)
        assert out["cache_hit"] is False
        assert out["docs_written"] == {"notes": 2}
        assert out["output"] == {"written": 2}
        assert out["evaluated_at"] == NOW  # a real aware datetime, not a string

    def test_dry_run_flag_forwarded(self, tmp_path, workspace):
        runner, app_store, data_store = _runner(tmp_path, workspace=workspace)
        pipeline = _code_pipeline()
        app_store.save(_app(pipeline=pipeline))
        out = runner.run_pipeline("app-x", "p", params={}, dry_run=True)
        assert out["docs_written"] == {"notes": 2}
        assert data_store.query("app-x", "notes") == []

    def test_unknown_app_raises(self, tmp_path):
        runner, _, _ = _runner(tmp_path)
        with pytest.raises(PipelineExecutionError) as exc:
            runner.run_pipeline("nope", "p", params={}, dry_run=False)
        assert exc.value.code == "not_found"

    def test_unknown_pipeline_raises(self, tmp_path):
        runner, app_store, _ = _runner(tmp_path)
        app_store.save(_app(pipeline=_code_pipeline()))
        with pytest.raises(PipelineExecutionError) as exc:
            runner.run_pipeline("app-x", "ghost", params={}, dry_run=False)
        assert exc.value.code == "not_found"

    def test_params_hash_is_stable_and_static(self):
        a = AppPipelineRunner.params_hash({"a": 1, "b": 2})
        b = AppPipelineRunner.params_hash({"b": 2, "a": 1})  # key order irrelevant
        assert a == b
        assert AppPipelineRunner.params_hash({}) != a


# ---------------------------------------------------------------------------
# The tracker code-fire seam
# ---------------------------------------------------------------------------


class _SpyFailureHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, PipelineIssue]] = []

    def handle_pipeline_failure(self, app: AppSpec, issue: PipelineIssue) -> None:
        self.calls.append((app.app_id, issue))


def _tracker(tmp_path, *, source, pipeline, handler=None, workspace=None):
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
    run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
    app = _app(pipeline=pipeline, source=source)
    app_store.save(app)
    runner = AppPipelineRunner(
        app_store=app_store, app_data=data_store,
        workspace_resolver=lambda _a: str(workspace) if workspace is not None else None,
        clock=lambda: NOW,
    )
    tracker = AppPipelineRunTracker(
        run_store=run_store, app_store=app_store, failure_handler=handler,
        pipeline_runner=runner, now_fn=lambda: NOW,
    )
    return tracker, app_store, data_store, run_store


class TestCodeFireSeam:
    def test_code_fire_runs_engine_and_writes_scheduled_ledger_row(self, tmp_path, workspace):
        pipeline = _code_pipeline(trigger_ref="trig-1")
        tracker, _, data_store, run_store = _tracker(
            tmp_path, source=_GLOB_PIPELINE, pipeline=pipeline, workspace=workspace
        )
        handled = tracker.run_code_pipeline_fire("trig-1", now=NOW)

        assert handled is True  # fire fully handled; _trigger_deliver returns True → NO re-engage
        rows = run_store.list_runs("app-x")
        assert len(rows) == 1
        run = rows[0]
        assert run.kind == "scheduled"
        assert run.status == "succeeded"
        assert run.cache == "miss"
        assert run.docs_written == {"notes": 2}
        assert run.params_hash == AppPipelineRunner.params_hash({})
        assert run.session_run_id is None  # never bound to a maintainer session
        # The engine actually wrote through the real data plane.
        assert len(data_store.query("app-x", "notes")) == 2

    def test_failed_code_fire_closes_failed_and_dispatches_policy(self, tmp_path):
        handler = _SpyFailureHandler()
        pipeline = _code_pipeline(trigger_ref="trig-1")
        tracker, _, _, run_store = _tracker(
            tmp_path,
            source="def run(params, ctx):\n    raise ValueError('boom')\n",
            pipeline=pipeline,
            handler=handler,
        )
        handled = tracker.run_code_pipeline_fire("trig-1", now=NOW)

        assert handled is True  # a failure is still "handled" (no maintainer wake)
        run = run_store.list_runs("app-x")[0]
        assert run.status == "failed"
        assert run.kind == "scheduled"
        assert "boom" in (run.error or "")
        assert handler.calls == [("app-x", PipelineIssue.run_failed("p", run.error))]

    def test_agentic_fire_is_not_handled_by_the_code_seam(self, tmp_path):
        # An agentic pipeline returns False → _trigger_deliver proceeds to re-engage.
        pipeline = PipelineSpec(name="p", wake_prompt="w", trigger_ref="trig-1", mode="agentic")
        tracker, _, _, run_store = _tracker(
            tmp_path, source="def run(params, ctx):\n    return 1\n", pipeline=pipeline
        )
        assert tracker.run_code_pipeline_fire("trig-1", now=NOW) is False
        assert run_store.list_runs("app-x") == []  # the code seam wrote nothing

    def test_non_app_fire_is_not_handled(self, tmp_path):
        pipeline = _code_pipeline(trigger_ref="trig-1")
        tracker, *_ = _tracker(tmp_path, source=_GLOB_PIPELINE, pipeline=pipeline)
        assert tracker.run_code_pipeline_fire("unknown-trigger", now=NOW) is False

    def test_unwired_runner_never_handles(self, tmp_path):
        app_store = JsonAppStore(root_dir=tmp_path / "apps")
        run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
        app_store.save(_app(pipeline=_code_pipeline(trigger_ref="trig-1")))
        tracker = AppPipelineRunTracker(
            run_store=run_store, app_store=app_store, pipeline_runner=None, now_fn=lambda: NOW
        )
        assert tracker.run_code_pipeline_fire("trig-1", now=NOW) is False

    def test_record_code_run_can_write_an_on_request_kind_row(self, tmp_path, workspace):
        # record_code_run CAN stamp kind="on_request" — the REST invoke endpoint
        # (Phase 2 revised ruling) now uses it, gated by require_effect.
        pipeline = _code_pipeline(trigger_ref="trig-1")
        tracker, _, _, run_store = _tracker(
            tmp_path, source=_GLOB_PIPELINE, pipeline=pipeline, workspace=workspace
        )
        app = tracker.app_store.get("app-x")
        run, result = tracker.record_code_run(app, pipeline, params={}, kind="on_request", now=NOW)
        assert run.kind == "on_request"
        assert run.status == "succeeded"
        assert run.docs_written == {"notes": 2}
        assert result is not None
        assert result.docs_written == {"notes": 2}


# ---------------------------------------------------------------------------
# Lifecycle submit — entrypoint-in-bundle validation
# ---------------------------------------------------------------------------


class _FakeSessions:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def create_session(self) -> str:
        return "maint-new"

    def tag_session(self, session_id: str, tag: str) -> None:
        pass

    def append_context_event(self, session_id: str, context: dict) -> None:
        pass

    def append_event(self, session_id: str, event: dict) -> None:
        self.events.append((session_id, event))


def _lifecycle(tmp_path):
    from mewbo_api.apps.lifecycle import AppLifecycle
    from mewbo_core.triggers.policy import TriggerPolicy
    from mewbo_core.triggers.store import JsonTriggerStore

    return AppLifecycle(
        app_store=JsonAppStore(root_dir=tmp_path / "apps"),
        trigger_store=JsonTriggerStore(data_file=tmp_path / "triggers.json"),
        trigger_policy=TriggerPolicy(),
        sessions=_FakeSessions(),
        now_fn=lambda: NOW,
    )


class TestSubmitCodePipelineValidation:
    def _draft(self, *, entrypoint_key: str, pipeline_entrypoint: str) -> AppSpec:
        pipeline = _code_pipeline(entrypoint=pipeline_entrypoint)
        return AppSpec(
            app_id="app-y",
            title="Y",
            owner_session_id="builder-1",
            workspace_ref=WorkspaceRef(kind="own", key="k"),
            frontend=AppFrontend(
                files={"app.py": "import streamlit as st", entrypoint_key: _GLOB_PIPELINE}
            ),
            collections=[CollectionSpec(name="notes", json_schema=_OPEN_SCHEMA)],
            pipelines=[pipeline],
            status="building",
        )

    def test_submit_rejects_entrypoint_not_in_bundle(self, tmp_path):
        lifecycle = _lifecycle(tmp_path)
        draft = self._draft(
            entrypoint_key="pipelines/other.py", pipeline_entrypoint="pipelines/p.py"
        )
        with pytest.raises(ValueError, match="not among the app bundle files"):
            lifecycle.submit(draft, builder_session_id="builder-1")

    def test_submit_accepts_entrypoint_present_in_bundle(self, tmp_path):
        lifecycle = _lifecycle(tmp_path)
        draft = self._draft(entrypoint_key="pipelines/p.py", pipeline_entrypoint="pipelines/p.py")
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert live.pipelines[0].mode == "code"
        assert live.pipelines[0].entrypoint == "pipelines/p.py"


class TestCodeFireIntegrityDispatch:
    """A code pipeline that runs clean and writes nothing must reach the policy too.

    The code tier's ``record_code_run`` is the second caller of the same dispatch,
    and it reuses the SAME ``dispatch_failure`` flag that already separates an
    autonomous fire from a manual invoke — so the manual-never-auto-repairs law
    covers integrity violations for free.
    """

    def _tracker_for(self, tmp_path, *, source, handler):
        pipeline = _code_pipeline(trigger_ref="trig-1")
        return _tracker(tmp_path, source=source, pipeline=pipeline, handler=handler)

    def test_a_scheduled_code_fire_that_regresses_a_collection_dispatches(self, tmp_path):
        handler = _SpyFailureHandler()
        writes = (
            "def run(params, ctx):\n"
            "    ctx.collection('notes').upsert('k', {'v': 1})\n"
            "    return 1\n"
        )
        tracker, app_store, _, run_store = self._tracker_for(
            tmp_path, source=writes, handler=handler
        )
        tracker.run_code_pipeline_fire("trig-1", now=NOW)  # baseline: writes to 'c'
        assert handler.calls == []

        # Same pipeline, now writing nothing — and still raising nothing.
        app = app_store.get("app-x")
        app_store.save(
            app.model_copy(
                update={
                    "frontend": app.frontend.model_copy(
                        update={
                            "files": {
                                **app.frontend.files,
                                "pipelines/p.py": "def run(params, ctx):\n    return 0\n",
                            }
                        }
                    )
                }
            )
        )
        tracker.run_code_pipeline_fire("trig-1", now=NOW + timedelta(hours=1))

        assert [i.kind for _, i in handler.calls] == ["unwritten_collections"]
        assert handler.calls[0][1].collections == ["notes"]
        # ...and both runs still tell the truth about themselves.
        assert {r.status for r in run_store.list_runs("app-x")} == {"succeeded"}

    def test_a_manual_rest_invoke_never_dispatches(self, tmp_path):
        handler = _SpyFailureHandler()
        writes = (
            "def run(params, ctx):\n"
            "    ctx.collection('notes').upsert('k', {'v': 1})\n"
            "    return 1\n"
        )
        tracker, app_store, _, _ = self._tracker_for(tmp_path, source=writes, handler=handler)
        tracker.run_code_pipeline_fire("trig-1", now=NOW)  # baseline

        app = app_store.get("app-x")
        empty = app.model_copy(
            update={
                "frontend": app.frontend.model_copy(
                    update={
                        "files": {
                            **app.frontend.files,
                            "pipelines/p.py": "def run(params, ctx):\n    return 0\n",
                        }
                    }
                )
            }
        )
        app_store.save(empty)
        # The REST-invoke flags: a user hammering a broken pipeline must not repair.
        tracker.record_code_run(
            empty,
            empty.pipelines[0],
            params={},
            kind="on_request",
            dispatch_failure=False,
            require_effect=True,
            now=NOW + timedelta(hours=1),
        )

        assert handler.calls == []
