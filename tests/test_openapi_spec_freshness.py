"""The committed REST reference describes every namespace the API can serve.

``docs/openapi.json`` is COMMITTED and GENERATED — captured from the live
Flask-RESTX schema by ``scripts/ci/generate_openapi_spec.py`` and rendered as
the Scalar reference by ``docs/rest-api.md``. Its historical defect was not
staleness but CONFIGURATION: ``backend.py`` mounts some namespaces only when a
feature is enabled and its infrastructure is present, so the published contract
was whatever the generating machine happened to have switched on, and a shipped
capability was simply missing from it. Nothing catches that by inspection — the
diff is empty precisely when the artifact is wrong.

The generator now pins its own configuration and mounts the gated namespaces
explicitly, so these tests are checking two different things:

- the committed artifact contains the gated paths (the regression itself);
- the inventory of RESTX namespaces in the app is the one the generator was
  written against, so a tenth namespace cannot be added, mounted behind a flag,
  and silently omitted the way the Web IDE was.

Nothing here writes. The freshness check shells out with ``--check``, which
writes nothing by contract, and runs in a SUBPROCESS on purpose: importing
``mewbo_api.backend`` re-registers namespaces on a shared Flask app and pins a
process-wide config, neither of which may leak into the rest of the suite.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = REPO_ROOT / "scripts" / "ci" / "generate_openapi_spec.py"
SPEC_PATH = REPO_ROOT / "docs" / "openapi.json"
API_SOURCE_ROOT = REPO_ROOT / "apps" / "mewbo_api" / "src" / "mewbo_api"
BACKEND_PATH = API_SOURCE_ROOT / "backend.py"

_REGENERATE = "uv run python scripts/ci/generate_openapi_spec.py"

# Every operation the Web IDE namespace serves. It is the one namespace
# ``backend.py`` mounts conditionally, and the one this suite exists for.
GATED_PATHS = (
    "/api/sessions/{session_id}/ide",
    "/api/sessions/{session_id}/ide/extend",
)

# The RESTX namespaces ``backend.py`` mounts unconditionally, by the variable
# each module assigns. Listed rather than derived: the point is that a NEW
# namespace fails this file and its author has to say which half it belongs to.
UNCONDITIONAL_NAMESPACES = frozenset(
    {
        ("mewbo_api.vcs_pickup", "vcs_ns"),
        ("mewbo_api.triggers.routes", "triggers_ns"),
        ("mewbo_api.agentic_search.routes", "agentic_ns"),
        ("mewbo_api.agentic_search.graph_routes", "graph_ns"),
        ("mewbo_api.realtime.routes", "draft_ns"),
        ("mewbo_api.structured.routes", "structured_ns"),
        ("mewbo_api.system_instructions.routes", "system_instructions_ns"),
        ("mewbo_api.apps.routes", "apps_ns"),
        # Speech is guarded on an optional PACKAGE rather than on config, so it
        # belongs here rather than in GATED_NAMESPACES for two reasons. It is
        # unconditional wherever the spec is generated — `mewbo-speech[gateway]`
        # is a dev-group workspace dependency, so every machine that can run this
        # suite has it — and GATED_NAMESPACES structurally cannot hold it: that
        # list is cross-checked against a literal `api.add_namespace(...)` call
        # in backend.py, while this namespace is registered inside
        # `init_speech_routes`, behind the import probe that keeps a genuine bug
        # in routes.py from being misreported as a missing extra.
        # If the package ever leaves the dev group, the `--check` gate below goes
        # red rather than the reference quietly shrinking — the loud failure is
        # the point.
        ("mewbo_api.speech.routes", "speech_ns"),
    }
)


def _load_generator():
    """Import the generator by path — ``scripts/`` is not a package.

    Its module body defines constants and a class; the import happens inside
    ``capture()`` and the write inside ``main()``, so this pulls in no app code
    and touches no file.
    """
    spec = importlib.util.spec_from_file_location("generate_openapi_spec", GENERATOR_PATH)
    assert spec and spec.loader, f"could not load {GENERATOR_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_namespaces() -> set[tuple[str, str]]:
    """Every ``<name> = Namespace(...)`` under ``mewbo_api``, as (module, attr).

    A source scan, never an import: importing the modules would drag in the
    whole app, and the question here is what the TREE declares, not what one
    configuration managed to construct.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(API_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        dotted = ".".join(path.relative_to(API_SOURCE_ROOT.parent).with_suffix("").parts)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            callee = node.value.func
            if not (isinstance(callee, ast.Name) and callee.id == "Namespace"):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.add((dotted, target.id))
    return found


def _backend_mount_paths(source: str | None = None) -> dict[str, str]:
    """Every ``api.add_namespace(<name>, path="...")`` in ``backend.py``.

    Keyed by the namespace VARIABLE, which is what ``GATED_NAMESPACES`` names
    too. *source* is a parameter so the drift case can be exercised on a
    modified copy without editing the tree.
    """
    tree = ast.parse(source if source is not None else BACKEND_PATH.read_text(encoding="utf-8"))
    mounts: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not (isinstance(callee, ast.Attribute) and callee.attr == "add_namespace"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        for keyword in node.keywords:
            if keyword.arg == "path" and isinstance(keyword.value, ast.Constant):
                # A non-literal path is not readable here; leaving it out makes
                # the caller's "no call site found" assertion fire, which is the
                # honest answer rather than a wrong one.
                if isinstance(keyword.value.value, str):
                    mounts[node.args[0].id] = keyword.value.value
    return mounts


class TestTheGeneratorMountsWhereTheAppWould:
    """The generator repeats a mount path; this is what keeps the copy honest.

    It cannot derive the path instead: a gated namespace is absent from the
    imported ``api.ns_paths`` precisely BECAUSE the configuration gated it off,
    which is true of every entry in ``GATED_NAMESPACES`` by definition. So the
    duplication stays and the drift is asserted here — publish operations at a
    prefix the server does not serve and this fails, rather than the reference
    quietly describing a path nobody can call.
    """

    def test_each_gated_namespace_mounts_at_backend_s_own_path(self):
        mounts = _backend_mount_paths()
        for module, attribute, path in _load_generator().GATED_NAMESPACES:
            assert attribute in mounts, (
                f"{attribute} ({module}) is listed as gated, but backend.py has no "
                "api.add_namespace call for it with a literal path — it was renamed, "
                "removed, or its mount path became a variable this check cannot read."
            )
            assert mounts[attribute] == path, (
                f"the generator mounts {attribute} at {path!r} while backend.py mounts "
                f"it at {mounts[attribute]!r}. The published reference would describe "
                "operations at a path the server does not serve. Update "
                "GATED_NAMESPACES in scripts/ci/generate_openapi_spec.py, then "
                "regenerate."
            )

    def test_the_check_fails_when_the_paths_diverge(self):
        # The tripwire, tripped: a check nobody has seen fail is not a check.
        drifted = _backend_mount_paths(
            BACKEND_PATH.read_text(encoding="utf-8").replace(
                'api.add_namespace(ide_ns, path="/api")',
                'api.add_namespace(ide_ns, path="/api/v2")',
            )
        )
        assert drifted["ide_ns"] == "/api/v2", (
            "the call site this test rewrites is no longer spelled that way in "
            "backend.py, so the drift case is not being exercised"
        )
        gated = {a: p for _m, a, p in _load_generator().GATED_NAMESPACES}
        assert drifted["ide_ns"] != gated["ide_ns"]


class TestTheReferenceCoversTheGatedNamespaces:
    """A capability the app can serve is described, whatever the local config."""

    def test_the_committed_spec_contains_the_gated_paths(self):
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        missing = [path for path in GATED_PATHS if path not in spec["paths"]]
        assert not missing, (
            f"docs/openapi.json omits {missing} — the Web IDE namespace is mounted "
            "only when the feature is enabled AND a Mongo-backed session store "
            "exists, so a spec generated without those describes a smaller API "
            "than the one that ships.\n"
            f"Regenerate it with:  {_REGENERATE}\n"
            "If the paths are still absent afterwards, the generator's "
            "GATED_NAMESPACES no longer names the namespace."
        )

    def test_the_gated_paths_carry_operations(self):
        # Guards the vacuous pass: a key present with no HTTP method documents
        # nothing, and the assertion above would not notice.
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        for path in GATED_PATHS:
            methods = [key for key in spec["paths"][path] if key in ("get", "post", "delete")]
            assert methods, f"{path} is in the spec with no operations"


class TestTheNamespaceInventoryIsTheOneTheGeneratorKnows:
    """A new RESTX namespace must be classified, not silently omitted."""

    def test_every_declared_namespace_is_gated_or_unconditional(self):
        gated = {
            (module, attribute)
            for module, attribute, _path in _load_generator().GATED_NAMESPACES
        }
        assert not (gated & UNCONDITIONAL_NAMESPACES), (
            "a namespace is listed as both gated and unconditional"
        )
        declared = _declared_namespaces()
        unaccounted = declared - gated - UNCONDITIONAL_NAMESPACES
        assert not unaccounted, (
            f"RESTX namespaces not accounted for: {sorted(unaccounted)}.\n"
            "Every namespace either mounts on every boot — add it to "
            "UNCONDITIONAL_NAMESPACES here — or mounts only under some "
            "configuration, in which case add it to GATED_NAMESPACES in "
            "scripts/ci/generate_openapi_spec.py so the reference documents it "
            "on machines where that configuration is off."
        )

    def test_the_inventory_has_not_lost_a_namespace(self):
        # The other direction: a namespace deleted or renamed leaves a stale
        # entry here, and the test above cannot see that.
        declared = _declared_namespaces()
        gated = {
            (module, attribute)
            for module, attribute, _path in _load_generator().GATED_NAMESPACES
        }
        stale = (gated | UNCONDITIONAL_NAMESPACES) - declared
        assert not stale, f"listed namespaces that no longer exist: {sorted(stale)}"


class TestTheCommittedSpecIsCurrent:
    """``--check`` is a usable gate only because generation is now deterministic."""

    def test_the_generator_pins_its_own_configuration(self):
        # The premise of the subprocess check below: if the generator stopped
        # pinning, a green run would only mean the machine happened to match.
        module = _load_generator()
        assert callable(module.OpenApiSpecExporter.pinned_config)
        assert module.GENERATION_CONFIG_PATH.exists(), (
            "the generation config is missing — it is a tracked file"
        )
        assert module.SPEC_OUTPUT_PATH == SPEC_PATH, (
            "the generator writes somewhere other than the file this test reads"
        )

    def test_the_committed_spec_matches_what_the_app_would_produce(self):
        # The subprocess pays a full app import (tens of seconds), so its own
        # timeout sits inside pytest's global one rather than outside it —
        # a generator that wedges must fail this test, not the whole run.
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(GENERATOR_PATH), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert result.returncode == 0, (
            "docs/openapi.json is STALE — it no longer matches the routes the API "
            f"registers.\nRegenerate it with:  {_REGENERATE}\n"
            f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
        )
