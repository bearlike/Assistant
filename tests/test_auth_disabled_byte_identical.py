"""Byte-identical-when-disabled: the API boots as it did before IAM existed.

The premise the whole IAM layer stands on is that `api.auth.enabled=false` (the
default — see `AuthKit`/`PermissionGuard` docstrings and
`apps/mewbo_api/CLAUDE.md` -> "Auth OFF must be byte-identical to before IAM
existed") makes a deployment indistinguishable from the pre-IAM branch point.
`tests/test_iam_architecture.py` already asserts pieces of this claim, but
in-process, against a hand-assembled `Flask(__name__)` carrying only the two
route groups a given test cares about — never the REAL `mewbo_api.backend.app`
object gunicorn actually serves, with every route, guard, and hook wired the
way boot wires them. That gap matters: several existing routes migrated from
an inline `_require_api_key()` call to the `@guard.requires(...)` decorator
(`auth/permission_guard.py`) during this branch, and a decorator migration is
exactly the kind of change that can silently reshape a response body while
every unit test keeps passing, because a unit test asserts what the code
DOES, not what a pre-existing client already depends on.

Every probe here therefore runs `mewbo_api.backend` in a FRESH subprocess (a
sibling of `test_iam_architecture.py`'s `_run_probe`, generalized to drive
real HTTP requests through `app.test_client()` instead of returning one
`sys.modules` fact) with its own clean `MEWBO_HOME`/`cwd` — never in-process,
where pytest has already imported half the world and a `sys.modules` or
on-disk assertion would pass no matter what the code under test does.

**What "no IAM modules are imported" honestly means.** `mewbo_iam` itself is a
non-optional, always-imported base dependency of `mewbo_api` (see its
`pyproject.toml`): `AuthKit` needs the kernel's `Principal`/`AuthSettings`
types to decide it is disabled in the first place, so `'mewbo_iam' in
sys.modules` is trivially `True` on every boot and asserting otherwise would
be a false bar, not a real one. The load-bearing claim — the one
`test_iam_architecture.py::test_importing_the_kernel_loads_no_driver_dependency`
already makes for a bare `import mewbo_iam` — is that the HEAVY DRIVER
payloads (`authlib`, `joserfc`, `ldap3`, `onelogin`/python3-saml) and the
kernel's own `mewbo_iam.drivers.*` submodules stay unimported through a full
app boot, not just through the kernel import alone.

The wire-body comparison (`test_disabled_auth_wire_bodies_match_origin_main_baseline`)
needs a REAL origin/main to diff against — not this file's understanding of
what origin/main does. `_origin_main_baseline_python` builds one from
`git archive origin/main` piped into a scratch directory, `uv sync`'d into its
own venv. In this checkout that resolves entirely from the local `uv` cache
(origin/main's dependency set is a strict subset of this branch's — nothing
IAM added was ever removed), so it needs no network here; a CI runner with a
shallow clone or a cold cache may not have `origin/main` reachable or the
wheels cached, so the fixture SKIPS with a clear reason rather than failing
the build on infrastructure it does not control — see its docstring.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# A fixed, obviously-fake credential shared by every probe so a "master token
# accepted" / "bad token rejected" comparison is apples-to-apples between the
# branch and the origin/main baseline, which know nothing of each other.
_TEST_MASTER_TOKEN = "msk-byte-identical-probe-token"  # noqa: S105 - fixture value, not a secret
_BAD_TOKEN = "mk_not_a_real_key_at_all"  # noqa: S105 - fixture value, not a secret

_PROBE_TIMEOUT_S = 120
_BASELINE_SETUP_TIMEOUT_S = 300


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def _clean_probe_env(mewbo_home: Path, *, master_token: str = _TEST_MASTER_TOKEN) -> dict[str, str]:
    """Build a minimal, explicit environment for a probe subprocess.

    Deliberately NOT `{**os.environ, ...}`: inheriting the parent's env risks
    a stray host `MEWBO_*`/`PYTHONPATH` var silently steering config or import
    resolution, which would make a failure a fact about this shell rather than
    about the code under test. `PATH` is the one thing worth keeping (the
    interpreter needs to find `git`/shared libs); `HOME` is redirected beside
    `MEWBO_HOME` so skill/plugin discovery cannot pick up the developer's real
    `~/.claude`.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(mewbo_home.parent / "userhome"),
        "MEWBO_HOME": str(mewbo_home),
        "MASTER_API_TOKEN": master_token,
    }


def _run_probe(
    python_exe: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    source: str,
    stdin_payload: str | None = None,
) -> dict[str, Any]:
    """Run *source* in a fresh interpreter, cwd/env pinned; return its parsed JSON stdout.

    Mirrors `test_iam_architecture.py::_run_probe` (only the last stdout line is
    parsed, so stray logging on stdout cannot corrupt the result) but adds
    `stdin_payload` — the wire-diff probe receives its request list this way
    rather than baking it into the source, so ONE probe body drives both the
    branch and the origin/main baseline app.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(python_exe), "-c", source],
        input=stdin_payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=_PROBE_TIMEOUT_S,
    )
    assert result.returncode == 0, (
        f"probe failed (exit {result.returncode})\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert lines, f"probe produced no stdout\n--- stderr ---\n{result.stderr}"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Probe A — full-app boot, real requests, store/module introspection.
# Branch-only: exercises IAM/SCIM/SAML surfaces that don't exist pre-IAM.
# ---------------------------------------------------------------------------

_COMPREHENSIVE_PROBE = r"""
import json
import os
import sys
from pathlib import Path

import mewbo_api.backend as backend

home = Path(os.environ["MEWBO_HOME"])
client = backend.app.test_client()


def do(method, path, headers=None):
    resp = client.open(path, method=method, headers=headers or {})
    return {"status": resp.status_code, "body": resp.get_json(silent=True)}


results = {
    "projects_no_cred": do("GET", "/api/projects"),
    "projects_bad_cred": do("GET", "/api/projects", {"X-API-Key": "%(bad_token)s"}),
    "projects_master": do("GET", "/api/projects", {"X-API-Key": "%(master_token)s"}),
    "sessions_master": do("GET", "/api/sessions", {"X-API-Key": "%(master_token)s"}),
    "iam_users": do("GET", "/api/iam/users"),
    "iam_roles": do("GET", "/api/iam/roles"),
    "scim_users": do("GET", "/api/scim/v2/Users"),
    "scim_spconfig": do("GET", "/api/scim/v2/ServiceProviderConfig"),
    "saml_metadata": do("GET", "/api/auth/saml/metadata"),
    "saml_login": do("GET", "/api/auth/saml/login"),
    "auth_oidc_login": do("GET", "/api/auth/login"),
    "auth_me": do("GET", "/api/auth/me"),
}

iam_files_after_requests = sorted(p.name for p in home.rglob("iam_*.json"))

# Non-vacuity: prove the glob watches the directory the stores would ACTUALLY
# resolve to by deliberately building one against the SAME MEWBO_HOME and
# confirming it appears — an empty result above only means something if this
# one does not stay empty too.
from mewbo_iam import create_role_store

create_role_store().list()
iam_files_after_deliberate_store = sorted(p.name for p in home.rglob("iam_*.json"))

iam_driver_modules = sorted(
    m for m in sys.modules if m.split(".")[0] in ("authlib", "joserfc", "ldap3", "onelogin")
)
mewbo_iam_driver_submodules = sorted(
    m for m in sys.modules if m.startswith("mewbo_iam.drivers.")
)

print(json.dumps({
    "results": results,
    "iam_files_after_requests": iam_files_after_requests,
    "iam_files_after_deliberate_store": iam_files_after_deliberate_store,
    "iam_driver_modules": iam_driver_modules,
    "mewbo_iam_driver_submodules": mewbo_iam_driver_submodules,
    "mewbo_iam_imported": "mewbo_iam" in sys.modules,
}))
"""


@pytest.fixture()
def _comprehensive_probe_result(tmp_path: Path) -> dict[str, Any]:
    """Run the comprehensive branch probe once; every assertion test shares it.

    One boot, several independent claims (modules / stores / routes) — sharing
    the run keeps the suite fast without weakening any individual assertion,
    since each claim reads a different, independent slice of the same JSON.
    """
    home = tmp_path / "home"
    env = _clean_probe_env(home)
    source = _COMPREHENSIVE_PROBE % {
        "bad_token": _BAD_TOKEN,
        "master_token": _TEST_MASTER_TOKEN,
    }
    return _run_probe(Path(sys.executable), cwd=tmp_path / "cwd", env=env, source=source)


# ---------------------------------------------------------------------------
# 1. No driver modules imported through a REAL app boot.
# ---------------------------------------------------------------------------


def test_disabled_auth_real_boot_imports_no_driver_modules(
    _comprehensive_probe_result: dict[str, Any],
) -> None:
    """A default `mewbo_api.backend` boot must not import any IAM driver payload.

    `mewbo_iam` itself IS expected to be resident (`mewbo_iam_imported` is
    asserted `True` below as a non-vacuity check on the probe, not as a
    violation — see the module docstring for why). What must NOT be resident
    is `authlib`/`joserfc` (OIDC), `ldap3` (LDAP), or `onelogin`/python3-saml
    (SAML) — the kernel test proves this for `import mewbo_iam` alone;
    this proves it survives the FULL app import graph (`saml_routes.py`,
    `oidc_runtime.py`, `federated.py`, `identity_flow.py` all import
    unconditionally at module scope), where a single lazy-loaded driver
    reached through a different path would defeat the optionality claim just
    as surely as one reached through the kernel directly.
    """
    result = _comprehensive_probe_result
    assert result["mewbo_iam_imported"] is True, (
        "the probe never even imported mewbo_iam — this test would prove nothing"
    )
    assert result["iam_driver_modules"] == [], (
        f"a default (auth-disabled) app boot loaded IAM driver payloads: "
        f"{result['iam_driver_modules']}"
    )
    assert result["mewbo_iam_driver_submodules"] == [], (
        f"a default (auth-disabled) app boot resolved kernel driver submodules: "
        f"{result['mewbo_iam_driver_submodules']}"
    )


# ---------------------------------------------------------------------------
# 2. No IAM store files, even after real requests.
# ---------------------------------------------------------------------------


def test_disabled_auth_real_boot_writes_no_iam_store_file(
    _comprehensive_probe_result: dict[str, Any],
) -> None:
    """Eleven real HTTP requests through the REAL app must write zero `iam_*.json` files.

    Unlike `test_iam_architecture.py::test_disabled_auth_writes_no_iam_store_file`
    (one hand-built route on a bare `Flask(__name__)`), this exercises the
    actual `mewbo_api.backend.app` — every `before_request` hook, every
    `@guard.requires(...)`-decorated route, a bad-credential attempt (which is
    exactly the path that would trip an audit-log write if
    `AuthKit._record_login_failure` were not itself gated on `enabled` — see
    `auth/kit.py:_audit`) — against its real, on-disk `MEWBO_HOME`.
    """
    result = _comprehensive_probe_result
    assert result["iam_files_after_requests"] == [], (
        f"a disabled deployment wrote IAM store files after real requests: "
        f"{result['iam_files_after_requests']}"
    )
    assert "iam_roles.json" in result["iam_files_after_deliberate_store"], (
        "the probe's glob is not watching the directory the IAM stores actually "
        "resolve to, so the empty result above would have proved nothing"
    )


# ---------------------------------------------------------------------------
# 3. No IAM/SCIM/SAML routes mounted; /api/auth/me stays reachable.
# ---------------------------------------------------------------------------


def test_disabled_auth_real_boot_mounts_no_iam_scim_saml_routes(
    _comprehensive_probe_result: dict[str, Any],
) -> None:
    """IAM/SCIM/SAML surfaces 404 on the REAL app; `/api/auth/me` still answers.

    A 403 here would still be wrong — it would mean the surface exists and
    merely refused the caller, which is a different, worse claim than "this
    surface is not mounted at all" (a probing caller could distinguish
    "disabled" from "exists, wrong role"). `/api/auth/me` is the one
    documented exception: the console calls it unconditionally to decide
    whether to render a login affordance.
    """
    result = _comprehensive_probe_result["results"]
    for key in (
        "iam_users",
        "iam_roles",
        "scim_users",
        "scim_spconfig",
        "saml_metadata",
        "saml_login",
        "auth_oidc_login",
    ):
        assert result[key]["status"] == 404, (
            f"{key} did not 404 when auth is disabled: {result[key]}"
        )

    me = result["auth_me"]
    assert me["status"] == 200, f"/api/auth/me did not stay reachable: {me}"
    assert me["body"]["auth_enabled"] is False
    assert me["body"]["authenticated"] is True


# ---------------------------------------------------------------------------
# 4. Wire-body diff against a REAL origin/main baseline.
# ---------------------------------------------------------------------------

_WIRE_DIFF_PROBE = r"""
import json
import sys

import mewbo_api.backend as backend

client = backend.app.test_client()
requests = json.loads(sys.stdin.read())
results = []
for req in requests:
    resp = client.open(
        req["path"], method=req.get("method", "GET"), headers=req.get("headers") or {}
    )
    results.append({"status": resp.status_code, "body": resp.get_json(silent=True)})
print(json.dumps(results))
"""

# Every request is picked to need NO session/LLM/project machinery — just the
# guard + a deterministic empty-store listing — so the ONLY thing that can
# make a body differ is a change to the guard or the handler itself.
_WIRE_DIFF_REQUESTS: list[dict[str, Any]] = [
    {"path": "/api/projects", "method": "GET"},
    {"path": "/api/projects", "method": "GET", "headers": {"X-API-Key": _BAD_TOKEN}},
    {"path": "/api/projects", "method": "GET", "headers": {"X-API-Key": _TEST_MASTER_TOKEN}},
    {"path": "/api/sessions", "method": "GET"},
    {"path": "/api/sessions", "method": "GET", "headers": {"X-API-Key": _BAD_TOKEN}},
    {"path": "/api/sessions", "method": "GET", "headers": {"X-API-Key": _TEST_MASTER_TOKEN}},
    {"path": "/api/this-route-does-not-exist-xyz", "method": "GET"},
]


@pytest.fixture(scope="session")
def _origin_main_baseline_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a real origin/main checkout with its own venv; return its python.

    `git archive origin/main | tar -x` into a scratch dir, then `uv sync
    --all-extras --all-groups` there — a genuinely separate installation, not
    a `sys.path` trick pointed at this worktree's editable installs (which
    would silently run origin/main's route code against THIS branch's
    `mewbo_core`/`mewbo_tools`, proving nothing about origin/main as it
    actually shipped). Skips (never fails the run) if `origin/main` is
    unreachable or `uv sync` cannot complete — the property this suite exists
    to prove only has evidentiary value when the baseline is real; a fallback
    to a weaker check would misrepresent that as passing.
    """
    if shutil.which("git") is None or shutil.which("uv") is None:
        pytest.skip("git or uv is not on PATH — cannot build an origin/main baseline")

    verify = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "origin/main"],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        pytest.skip(
            "origin/main is not a reachable ref in this checkout (shallow clone?): "
            f"{verify.stderr.strip()}"
        )

    baseline_root = tmp_path_factory.mktemp("origin_main_baseline")
    archive = subprocess.Popen(
        ["git", "-C", str(REPO_ROOT), "archive", "origin/main"], stdout=subprocess.PIPE
    )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(baseline_root)],
        stdin=archive.stdout,
        capture_output=True,
        text=True,
    )
    assert archive.stdout is not None
    archive.stdout.close()
    archive.wait()
    if archive.returncode != 0 or extract.returncode != 0:
        pytest.skip(
            f"could not extract an origin/main archive "
            f"(git exit {archive.returncode}, tar exit {extract.returncode}): {extract.stderr}"
        )

    try:
        sync = subprocess.run(
            ["uv", "sync", "--all-extras", "--all-groups"],
            cwd=str(baseline_root),
            capture_output=True,
            text=True,
            timeout=_BASELINE_SETUP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(
            f"uv sync for the origin/main baseline did not finish within "
            f"{_BASELINE_SETUP_TIMEOUT_S}s (likely a cold cache needing network)"
        )
    if sync.returncode != 0:
        pytest.skip(f"uv sync for the origin/main baseline failed:\n{sync.stderr[-2000:]}")

    baseline_python = baseline_root / ".venv" / "bin" / "python"
    if not baseline_python.exists():
        pytest.skip(f"uv sync did not produce a venv interpreter at {baseline_python}")
    return baseline_python


def test_disabled_auth_wire_bodies_match_origin_main_baseline(
    tmp_path: Path, _origin_main_baseline_python: Path
) -> None:
    """Every representative response is byte-for-byte identical to origin/main.

    Both `/api/projects` and `/api/sessions` GET handlers migrated from an
    inline `_require_api_key()` call to `@guard.requires("projects.read")` /
    `@guard.requires("sessions.read_all")` (`auth/permission_guard.py`) on
    this branch — exactly the kind of refactor that can reshape a response
    while every unit test targeting the NEW code keeps passing. This is the
    one check in the file that would catch it: the same requests, against the
    same clean-slate config, on the actual pre-IAM code.
    """
    stdin_payload = json.dumps(_WIRE_DIFF_REQUESTS)

    branch_home = tmp_path / "branch_home"
    branch_env = _clean_probe_env(branch_home)
    branch_results = _run_probe(
        Path(sys.executable),
        cwd=tmp_path / "branch_cwd",
        env=branch_env,
        source=_WIRE_DIFF_PROBE,
        stdin_payload=stdin_payload,
    )

    baseline_home = tmp_path / "baseline_home"
    baseline_env = _clean_probe_env(baseline_home)
    baseline_results = _run_probe(
        _origin_main_baseline_python,
        cwd=tmp_path / "baseline_cwd",
        env=baseline_env,
        source=_WIRE_DIFF_PROBE,
        stdin_payload=stdin_payload,
    )

    assert len(branch_results) == len(baseline_results) == len(_WIRE_DIFF_REQUESTS)
    mismatches = []
    for req, branch_r, baseline_r in zip(
        _WIRE_DIFF_REQUESTS, branch_results, baseline_results, strict=True
    ):
        if branch_r != baseline_r:
            mismatches.append(
                f"{req.get('method', 'GET')} {req['path']} "
                f"(headers={req.get('headers')}):\n"
                f"  branch:   {branch_r}\n"
                f"  baseline: {baseline_r}"
            )
    assert not mismatches, "wire body drift vs origin/main:\n" + "\n".join(mismatches)
