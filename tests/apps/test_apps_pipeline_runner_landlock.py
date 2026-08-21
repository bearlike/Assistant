"""Landlock scoping for the ``ctx.exec`` seam — ``PipelineExecutor.run``.

Proves the fix for a spawn seam that bypassed workspace scoping:
``PipelineExecutor.run`` passes its OWN ``workspace_root`` as the active root
to ``ShellScope.for_active_root``,
so an app bound to a ``kind="shared"`` configured project keeps that project
reachable while every OTHER configured project stays denied — the same
DENY-list mechanism ``tests/test_shell_landlock.py`` exercises for the shell
tool, applied at a second spawn seam. Style mirrors that file: real processes
through a real ``PipelineExecutor``, skipped when Landlock is unavailable.

``PipelineSpec.check_exec_allowed``/``_check_git_shape`` (exercised in
``test_apps_pipeline_runner.py::TestGitArgvShapeGate``) refuse ``git -C <dir>``
at the AUTHORIZATION layer before a subprocess is ever spawned — by design, a
code pipeline cannot point ``git`` at an arbitrary directory via argv at all.
So the argv-authorized path can only prove the POSITIVE case (the pipeline's
own workspace stays usable); proving the NEGATIVE case (a different project is
refused) needs an argv the authorization layer would reject in production,
deliberately bypassed here (`monkeypatch` on the class method) so those tests
isolate exactly the new composition — ``ShellScope.for_active_root(workspace_root)``
wrapped around ``PipelineExecutor.run``'s ``subprocess.Popen`` call — from the
separately-tested authorization rule. A plain python subprocess (rather than
git) is the probe for those: git's own repository-discovery walk masks a
Landlock denial as a generic "not a git repository" (it only ``stat()``s,
which Landlock does not gate — a directory *listing* or file *read* is what
actually trips ``READ_DIR``/``READ_FILE``, confirmed against ``os.listdir``
directly), so it cannot itself distinguish "denied" from "not a repo" — an
open()+read() can.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from mewbo_api.apps.models import PipelineSpec
from mewbo_api.apps.pipeline_runner import PipelineExecutor
from mewbo_core.config import reset_config, set_config_override
from mewbo_core.contracts.secret_redaction import get_secret_redactor
from mewbo_tools.integration.landlock import LandlockAbi

from apps.test_apps_pipeline_runner import _code_pipeline

_HAS_LANDLOCK = sys.platform.startswith("linux") and LandlockAbi.probe().available
requires_landlock = pytest.mark.skipif(
    not _HAS_LANDLOCK, reason="Landlock is a Linux-only kernel feature and is unavailable here"
)


def _read_probe(path) -> list[str]:
    """argv for a python subprocess that reads *path* and reports the outcome.

    Prints the file's content on success; on any ``OSError`` (Landlock's denial
    surfaces as ``PermissionError``, a subclass) writes to stderr and exits 1 —
    an unambiguous signal a git-based probe cannot give (see module docstring).
    """
    return [
        sys.executable,
        "-c",
        (
            "import sys\n"
            "try:\n"
            "    sys.stdout.write(open(sys.argv[1]).read())\n"
            "except OSError as exc:\n"
            "    sys.stderr.write(str(exc))\n"
            "    sys.exit(1)\n"
        ),
        str(path),
    ]


@pytest.fixture
def projects(tmp_path):
    """Two configured-project checkouts; ``alpha`` is also a real git repo."""
    made = {}
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        (d / "secret.env").write_text(f"SECRET_{name.upper()}=value\n")
        made[name] = d
    subprocess.run(["git", "init", "-q"], cwd=made["alpha"], check=True)
    return made


def _configure(projects_map, *, shell_sandbox: bool = True) -> None:
    set_config_override(
        {
            "agent": {"shell_sandbox": shell_sandbox},
            "projects": {k: {"path": str(v)} for k, v in projects_map.items()},
        }
    )


def _executor(pipeline, workspace_root) -> PipelineExecutor:
    return PipelineExecutor(
        pipeline=pipeline, workspace_root=workspace_root, redactor=get_secret_redactor()
    )


class TestPipelineExecutorLandlockScope:
    def teardown_method(self):
        reset_config()

    @requires_landlock
    def test_the_workspace_root_stays_reachable_through_real_authorized_argv(self, projects):
        # No bypass: real `PipelineSpec.check_exec_allowed` + a `git` argv it
        # actually admits (`status`), proving the realistic path end to end.
        _configure(projects)
        pipeline = _code_pipeline(allow_exec=["git"])
        result = _executor(pipeline, projects["alpha"]).run(["git", "status"])
        assert result["returncode"] == 0, result["stderr"]

    @requires_landlock
    def test_the_workspace_root_stays_reachable(self, projects, monkeypatch):
        _configure(projects)
        monkeypatch.setattr(
            PipelineSpec, "check_exec_allowed", lambda self, argv, *, allowed_binaries: None
        )
        pipeline = _code_pipeline(allow_exec=["git"])
        result = _executor(pipeline, projects["alpha"]).run(
            _read_probe(projects["alpha"] / "secret.env")
        )
        assert result["returncode"] == 0, result["stderr"]
        assert "SECRET_ALPHA" in result["stdout"]

    @requires_landlock
    def test_a_different_configured_project_is_refused(self, projects, monkeypatch):
        # The pipeline's workspace is alpha; beta is a DIFFERENT configured
        # project and must stay unreachable from a spawn scoped to alpha.
        _configure(projects)
        monkeypatch.setattr(
            PipelineSpec, "check_exec_allowed", lambda self, argv, *, allowed_binaries: None
        )
        pipeline = _code_pipeline(allow_exec=["git"])
        result = _executor(pipeline, projects["alpha"]).run(
            _read_probe(projects["beta"] / "secret.env")
        )
        assert result["returncode"] != 0
        assert "Permission" in result["stderr"]
        assert "SECRET_BETA" not in result["stdout"]

    @requires_landlock
    def test_flag_off_leaves_the_spawn_unscoped(self, projects, monkeypatch):
        # `agent.shell_sandbox` off ⇒ `ShellScope.for_active_root` returns
        # `None` ⇒ `scoped_preexec` degrades to `nullcontext(None)` — byte
        # identical to the pre-scoping spawn. Proven by reaching `beta` clean.
        _configure(projects, shell_sandbox=False)
        monkeypatch.setattr(
            PipelineSpec, "check_exec_allowed", lambda self, argv, *, allowed_binaries: None
        )
        pipeline = _code_pipeline(allow_exec=["git"])
        result = _executor(pipeline, projects["alpha"]).run(
            _read_probe(projects["beta"] / "secret.env")
        )
        assert result["returncode"] == 0, result["stderr"]
        assert "SECRET_BETA" in result["stdout"]
