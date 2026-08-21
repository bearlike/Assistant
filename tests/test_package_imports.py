"""Every distributed package imports — asserted in a fresh interpreter per package.

This is NOT a feature test. It pins one property the suite structurally cannot
prove about itself: that each shipped package can be imported at all.

**A green suite is evidence about the tests, not about the packages.** The suite
covers what the suite imports, and coverage of the module surface is uneven —
`mewbo_mcp` has very little. A package once reached the main branch holding a
module that named a moved import at its old path: the package raised
`ModuleNotFoundError` on import, and the suite stayed green, because no test
imported that module. It was found by a person reading code, not by a gate.

**A subprocess per package is the point, not an implementation detail.** An
in-process loop shares ``sys.modules``, so whichever package imported first
satisfies the imports of every package checked after it, and the ordering
problems the check exists to find are exactly the ones it would hide. Such a
check passes and proves nothing. :meth:`FreshInterpreterProbe.run` therefore
spends a whole interpreter per package, and
``test_each_package_was_probed_in_a_genuinely_fresh_interpreter`` asserts the
isolation actually held rather than assuming it.

**Package roots are not enough.** Four of the eight root ``__init__.py`` files
are docstring-only by deliberate design — a subpackage that re-exports its
modules makes importing one of them execute all of them, which changes what
module-level side effects fire and in what order. So ``import mewbo_core``
exercises almost nothing. Every module is imported individually instead.

The complementary defect class — a packaged asset resolved relative to a
module's own position, which breaks when that module moves and fails at render
time rather than import time — is invisible here by construction, because this
gate is about imports and that is a filesystem path. It has its own gate in
``test_packaged_resource_anchors.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]

# Generous: the point of the bound is to turn a hung import into a named failure
# rather than a suite that never finishes. A real probe takes ~20s at worst.
PROBE_TIMEOUT_S = 300

# The probe body. It is deliberately trivial — it reads the module list from
# stdin rather than deriving it, so the discovery rule stays in real, linted,
# type-checked, separately-tested Python (:class:`DistributedPackage`) instead of
# inside a string that no tool inspects.
#
# `BaseException`, not `Exception`: a module body is allowed to raise
# `SystemExit`, and a probe that let that through would report a clean exit for
# a module that never imported. `test_the_probe_reports_a_module_that_exits_on_
# import` pins that.
_PROBE_SOURCE = """
import importlib
import json
import sys
import traceback

payload = json.loads(sys.stdin.read())

# Computed BEFORE the first import, so it answers "what was already resident
# when this interpreter started" rather than "what did we just import".
preloaded = sorted(
    name for name in sys.modules if name.split(".")[0] in payload["distributions"]
)

failures = []
for name in payload["modules"]:
    try:
        importlib.import_module(name)
    except BaseException as exc:  # a probe reports; it never re-raises
        failures.append(
            {
                "module": name,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "traceback": traceback.format_exc()[-2000:],
            }
        )

print(json.dumps({"preloaded": preloaded, "failures": failures}))
"""


class ModuleImportFailure(BaseModel):
    """One module that could not be imported, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str
    error_type: str
    error: str
    traceback: str

    def summary(self) -> str:
        """One line naming the module and its error, for an assertion message."""
        return f"{self.module}: {self.error_type}: {self.error}"


class PackageImportReport(BaseModel):
    """What one fresh interpreter found while importing one package.

    A Pydantic model with ``extra="forbid"`` because this crosses a process
    boundary: the probe's stdout is parsed JSON from another interpreter, and a
    field this side stops reading — or a probe that silently starts reporting a
    different shape — should be a loud validation error rather than a key that
    quietly reads as absent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: str
    imported: tuple[str, ...]
    skipped: tuple[str, ...]
    preloaded: tuple[str, ...]
    failures: tuple[ModuleImportFailure, ...]


class DistributedPackage(BaseModel):
    """One shipped package: its import name, its source root, and its modules.

    Discovery is here rather than in the probe so it is ordinary code — linted,
    type-checked, and driven directly by
    ``test_discovery_skips_exactly_the_files_that_are_not_modules``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    import_name: str
    src_root: Path

    def module_name(self, path: Path) -> str:
        """Dotted module name for *path* (``__init__.py`` names its package)."""
        parts = list(path.relative_to(self.src_root).parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1].removesuffix(".py")
        return ".".join([self.import_name, *parts])

    def _is_module(self, path: Path) -> bool:
        """True when every directory up to the package root has an ``__init__.py``.

        This is the whole exclusion rule, and it is a fact about the tree rather
        than a judgement about the file: a ``.py`` file whose parent directory
        has no ``__init__.py`` has no dotted name, so there is nothing for this
        gate to import. It is what keeps the example scripts that run Streamlit
        in their module body — and the SDK source that is delivered to a browser
        as text rather than imported — out of the sweep without a hand-curated
        list of names to keep in step with the tree.

        The exclusion is not taken on trust either: the set it produces is
        asserted exactly, so a real package directory losing its ``__init__.py``
        (which would silently drop every module under it out of the gate) fails
        as loudly as a new example script would.
        """
        directory = path.parent
        while directory != self.src_root:
            if not (directory / "__init__.py").exists():
                return False
            directory = directory.parent
        return True

    def _walk(self) -> list[tuple[str, bool]]:
        """Every ``.py`` file under the root as ``(dotted name, is a module)``."""
        return [
            (self.module_name(path), self._is_module(path))
            for path in sorted(self.src_root.rglob("*.py"))
        ]

    def modules(self) -> tuple[str, ...]:
        """Every importable module of this package, in a stable order."""
        return tuple(name for name, importable in self._walk() if importable)

    def skipped(self) -> tuple[str, ...]:
        """Every ``.py`` file under the root that is not a module of the package."""
        return tuple(name for name, importable in self._walk() if not importable)


# The eight packages this repository distributes. `mewbo_ha_conversation` is
# deliberately absent: it is a path source rather than a workspace member
# because it depends on Home Assistant, whose Python floor cannot intersect this
# workspace's, so it is not installed in the environment the suite runs in and
# an import probe would report an absence that is by design. It deploys
# out-of-band and is checked where it is deployed.
DISTRIBUTED_PACKAGES: tuple[DistributedPackage, ...] = (
    DistributedPackage(
        import_name="mewbo_core",
        src_root=REPO_ROOT / "packages" / "mewbo_core" / "src" / "mewbo_core",
    ),
    DistributedPackage(
        import_name="mewbo_tools",
        src_root=REPO_ROOT / "packages" / "mewbo_tools" / "src" / "mewbo_tools",
    ),
    DistributedPackage(
        import_name="mewbo_graph",
        src_root=REPO_ROOT / "packages" / "mewbo_graph" / "src" / "mewbo_graph",
    ),
    DistributedPackage(
        import_name="mewbo_iam",
        src_root=REPO_ROOT / "packages" / "mewbo_iam" / "src" / "mewbo_iam",
    ),
    DistributedPackage(
        import_name="mewbo_api",
        src_root=REPO_ROOT / "apps" / "mewbo_api" / "src" / "mewbo_api",
    ),
    DistributedPackage(
        import_name="mewbo_cli",
        src_root=REPO_ROOT / "apps" / "mewbo_cli" / "src" / "mewbo_cli",
    ),
    DistributedPackage(
        import_name="mewbo_mcp",
        src_root=REPO_ROOT / "apps" / "mewbo_mcp" / "src" / "mewbo_mcp",
    ),
    DistributedPackage(
        import_name="mewbo_speech",
        src_root=REPO_ROOT / "packages" / "mewbo_speech" / "src" / "mewbo_speech",
    ),
)

# Every `.py` file under a package root that is NOT a module of that package,
# stated exactly. All of them sit in a directory with no `__init__.py`, which is
# what makes them unreachable by a dotted name:
#
#   * the `examples/` trees are Streamlit scripts and preview components — their
#     module bodies call `st.title()` / `st.stop()`, so "importing" one runs a
#     page rather than binding a module;
#   * `apps/plugin/sdk/mewbo_app.py` is the app SDK, read as TEXT and injected
#     into a browser kernel at render time. It is never imported in-process.
#
# Pinned as an exact set, so both directions are loud. A new example script
# lands here as a one-line diff. An `__init__.py` disappearing from a REAL
# package directory — which would silently drop every module beneath it out of
# the gate, the precise way a check stops checking without anyone noticing —
# fails instead.
NOT_PACKAGE_MODULES: frozenset[str] = frozenset(
    {
        "mewbo_core.builtin_plugins.widget_builder.examples.components.github_repo_card",
        "mewbo_core.builtin_plugins.widget_builder.examples.components.plantuml_card",
        "mewbo_core.builtin_plugins.widget_builder.examples.components.search_result_card",
        "mewbo_core.builtin_plugins.widget_builder.examples.components.stock_ticker_card",
        "mewbo_core.builtin_plugins.widget_builder.examples.data_table.app",
        "mewbo_core.builtin_plugins.widget_builder.examples.finance_chart.app",
        "mewbo_api.apps.plugin.examples.components.data_table_page",
        "mewbo_api.apps.plugin.examples.components.freshness_strip",
        "mewbo_api.apps.plugin.examples.components.metric_header",
        "mewbo_api.apps.plugin.examples.email_organizer.app",
        "mewbo_api.apps.plugin.examples.email_organizer.pages.all_mail",
        # The app-builder recipe cookbook: pipeline sources an agent reads as
        # worked examples, executed only by the pipeline runner in its own
        # curated namespace. They are not importable here by construction — the
        # runner's allowlist bans what a normal import needs — and their
        # directories are hyphenated so nothing can accidentally import them.
        "mewbo_api.apps.plugin.examples.recipes.cli-json.repos",
        "mewbo_api.apps.plugin.examples.recipes.cli-text.branches",
        "mewbo_api.apps.plugin.examples.recipes.files-to-collection.expenses",
        "mewbo_api.apps.plugin.examples.recipes.llm-transform.triage",
        "mewbo_api.apps.plugin.examples.recipes.user-input.add_note",
        "mewbo_api.apps.plugin.examples.recipes.verifier.summary",
        "mewbo_api.apps.plugin.examples.recipes.verifier.verify_summary",
        "mewbo_api.apps.plugin.sdk.mewbo_app",
    }
)

# A floor, not a count — the exact figure moves with ordinary work, but an order
# of magnitude below this means discovery broke and the gate is sweeping air.
MINIMUM_MODULES_SWEPT = 350


class FreshInterpreterProbe:
    """Imports one package's modules in a subprocess and parses what it reports.

    A plain class, not a Pydantic model: it is in-process test machinery that
    crosses no trust boundary. Its *output* is the model, because that is what
    arrives from another interpreter.

    Collaborators are injected so the probe can be pointed at a package built by
    a test — which is how ``test_the_probe_reports_a_module_that_cannot_be_
    imported`` proves the gate is able to fail at all.
    """

    def __init__(
        self,
        *,
        home_root: Path,
        interpreter: Path | None = None,
        repo_root: Path = REPO_ROOT,
        timeout_s: int = PROBE_TIMEOUT_S,
        sys_path: tuple[Path, ...] = (),
    ) -> None:
        """Bind the interpreter, the throwaway home, and any extra search path."""
        self.home_root = home_root
        self.interpreter = interpreter or Path(sys.executable)
        self.repo_root = repo_root
        self.timeout_s = timeout_s
        self.sys_path = sys_path

    def _env(self, package: DistributedPackage) -> dict[str, str]:
        """Environment for one probe: its own throwaway home, no ambient config.

        ``MEWBO_HOME`` is load-bearing rather than hygiene. Importing these
        packages CREATES DIRECTORIES — a bare ``import mewbo_api`` in a fresh
        interpreter lays down ``cache/`` and ``sessions/`` under whatever home
        resolves — so without this every run of the gate would write into the
        developer's real one. Each package gets its OWN subdirectory, because
        the probes run concurrently and a shared home is shared mutable state.

        ``MEWBO_CONFIG`` is dropped for the same reason a test never reads the
        developer's config: whether a package imports must not depend on what
        happens to be on this machine.
        """
        env = {key: value for key, value in os.environ.items() if key != "MEWBO_CONFIG"}
        env["MEWBO_HOME"] = str(self.home_root / package.import_name)
        if self.sys_path:
            existing = env.get("PYTHONPATH")
            entries = [str(path) for path in self.sys_path]
            env["PYTHONPATH"] = os.pathsep.join([*entries, existing] if existing else entries)
        return env

    def run(self, package: DistributedPackage) -> PackageImportReport:
        """Import every module of *package* in a new interpreter; return its report."""
        modules = package.modules()
        payload = json.dumps(
            {
                "modules": list(modules),
                "distributions": [pkg.import_name for pkg in DISTRIBUTED_PACKAGES],
            }
        )
        try:
            completed = subprocess.run(
                [str(self.interpreter), "-c", _PROBE_SOURCE],
                input=payload,
                capture_output=True,
                text=True,
                cwd=str(self.repo_root),
                env=self._env(package),
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return PackageImportReport(
                package=package.import_name,
                imported=modules,
                skipped=package.skipped(),
                preloaded=(),
                failures=(
                    ModuleImportFailure(
                        module=package.import_name,
                        error_type="TimeoutExpired",
                        error=f"probe did not finish within {self.timeout_s}s",
                        traceback="",
                    ),
                ),
            )

        # Only the LAST line is parsed: these packages log on import, and stray
        # output must not be able to corrupt the report. No parseable line at all
        # means the interpreter died outright — a module body calling `os._exit`
        # or segfaulting a native extension — which is a real failure of the
        # property under test and must not read as an empty success.
        report = self._parse(completed.stdout)
        assert report is not None, (
            f"the {package.import_name} probe produced no report "
            f"(exit {completed.returncode}) — the interpreter did not survive "
            f"importing it.\nstdout:\n{completed.stdout[-2000:]}\n"
            f"stderr:\n{completed.stderr[-2000:]}"
        )
        return PackageImportReport(
            package=package.import_name,
            imported=modules,
            skipped=package.skipped(),
            preloaded=tuple(report["preloaded"]),
            failures=tuple(ModuleImportFailure(**entry) for entry in report["failures"]),
        )

    @staticmethod
    def _parse(stdout: str) -> dict[str, Any] | None:
        """The last line of *stdout* as JSON, or ``None`` if there is not one."""
        for line in reversed(stdout.strip().splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, PackageImportReport]:
    """Probe all eight packages concurrently; one report each.

    Concurrent because the probes are independent by construction — that is the
    property being tested — and because sequential runs cost ~61s against ~19s
    here. Each still gets its own interpreter and its own home directory, so
    concurrency changes the wall clock and nothing else.
    """
    home_root = tmp_path_factory.mktemp("probe-homes")
    probe = FreshInterpreterProbe(home_root=home_root)
    with ThreadPoolExecutor(max_workers=len(DISTRIBUTED_PACKAGES)) as pool:
        collected = list(pool.map(probe.run, DISTRIBUTED_PACKAGES))
    return {report.package: report for report in collected}


@pytest.mark.parametrize(
    "package", DISTRIBUTED_PACKAGES, ids=lambda pkg: pkg.import_name
)
def test_every_module_of_every_distributed_package_imports(
    package: DistributedPackage, reports: dict[str, PackageImportReport]
) -> None:
    """The gate. Every module of this package imports in an interpreter of its own.

    A failure here names the module and the exception. The usual cause is an
    import naming a path that no longer exists — a module moved and a reference
    to its old location left behind — which raises `ModuleNotFoundError` on
    import and nothing else, and which no amount of test coverage elsewhere in
    the package will surface if no test happens to import that one module.
    """
    report = reports[package.import_name]
    assert report.failures == (), (
        f"{package.import_name} has {len(report.failures)} module(s) that do not "
        f"import:\n"
        + "\n".join(f"  {failure.summary()}" for failure in report.failures)
        + "\n\nfirst traceback:\n"
        + (report.failures[0].traceback if report.failures else "")
    )


def test_each_package_was_probed_in_a_genuinely_fresh_interpreter(
    reports: dict[str, PackageImportReport],
) -> None:
    """No probe found another distributed package already resident.

    The isolation is the entire method, so it is asserted rather than assumed.
    If this ever fails, every other assertion in this file weakens at the same
    moment: a package that imports only because a sibling was imported first
    reports success it has not earned, which is precisely the failure this gate
    was written to end.
    """
    contaminated = {
        name: report.preloaded for name, report in reports.items() if report.preloaded
    }
    assert contaminated == {}, (
        "a probe started with distributed packages already in sys.modules, so it "
        f"was not measuring import health in isolation: {contaminated}"
    )


def test_discovery_skips_exactly_the_files_that_are_not_modules(
    reports: dict[str, PackageImportReport],
) -> None:
    """The excluded set is exactly ``NOT_PACKAGE_MODULES`` — loud in both directions.

    An exclusion rule that quietly grows is how a gate stops being a gate. The
    dangerous direction is not a new example script; it is an `__init__.py`
    vanishing from a real package directory, which drops every module beneath it
    out of the sweep while every test here stays green.
    """
    skipped = {name for report in reports.values() for name in report.skipped}
    assert skipped == NOT_PACKAGE_MODULES, (
        "the set of files excluded from the import sweep changed.\n"
        f"  newly excluded: {sorted(skipped - NOT_PACKAGE_MODULES)}\n"
        f"  no longer excluded: {sorted(NOT_PACKAGE_MODULES - skipped)}"
    )


def test_the_sweep_covers_a_plausible_number_of_modules(
    reports: dict[str, PackageImportReport],
) -> None:
    """Non-vacuity: discovery found a real tree, not an empty one.

    Every assertion above is over a set that discovery produced. A rglob against
    a mistyped root returns nothing, and "no module failed to import" is
    trivially true of no modules — so the size of the swept set is asserted too,
    and per package, because one root going empty would otherwise hide inside a
    healthy total.
    """
    swept = {name: len(report.imported) for name, report in reports.items()}
    assert all(count > 0 for count in swept.values()), f"a package swept no modules: {swept}"
    assert sum(swept.values()) >= MINIMUM_MODULES_SWEPT, (
        f"the sweep found only {sum(swept.values())} modules across {len(swept)} "
        f"packages, expected at least {MINIMUM_MODULES_SWEPT}: {swept}"
    )


def _build_package(root: Path, name: str, modules: dict[str, str]) -> DistributedPackage:
    """Write a throwaway importable package under *root* and describe it."""
    src = root / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text('"""Fixture package."""\n', encoding="utf-8")
    for module, body in modules.items():
        (src / f"{module}.py").write_text(body, encoding="utf-8")
    return DistributedPackage(import_name=name, src_root=src)


def test_the_probe_reports_a_module_that_cannot_be_imported(tmp_path: Path) -> None:
    """PROOF THE GATE CAN FAIL — the assertion this whole file rests on.

    A check that cannot go red is decoration, and this one exists because a
    green suite was once mistaken for a working check. So the probe is pointed
    at a package built here containing the exact defect that shipped: a module
    importing a name that does not exist.

    Asserted permanently and in the suite, rather than demonstrated once by hand
    breaking a real package, because a one-off demonstration proves the gate
    worked on the day someone tried it and nothing about the day it matters.
    """
    package = _build_package(
        tmp_path,
        "probefixture_broken",
        {
            "fine": "VALUE = 1\n",
            "broken": "from probefixture_broken.moved_away import Gone\n",
        },
    )
    probe = FreshInterpreterProbe(home_root=tmp_path / "home", sys_path=(tmp_path,))

    report = probe.run(package)

    assert [failure.module for failure in report.failures] == ["probefixture_broken.broken"], (
        "the probe did not report the broken module — it cannot fail, so it "
        f"proves nothing: {report.failures}"
    )
    assert report.failures[0].error_type == "ModuleNotFoundError"
    assert "probefixture_broken.fine" in report.imported


def test_the_probe_reports_a_module_that_exits_on_import(tmp_path: Path) -> None:
    """A module body raising ``SystemExit`` is a failure, not a clean run.

    ``SystemExit`` does not inherit from ``Exception``, so a probe catching
    ``Exception`` would let it unwind, end the interpreter with a zero status,
    and produce no report at all — which the parser would have to treat as
    either a crash or, worse, a success. The packages here really do contain
    scripts that exit in their module body; they are excluded because they are
    not modules, but the probe must not depend on that having been done right.
    """
    package = _build_package(
        tmp_path,
        "probefixture_exits",
        {"quits": "import sys\n\nsys.exit(3)\n"},
    )
    probe = FreshInterpreterProbe(home_root=tmp_path / "home", sys_path=(tmp_path,))

    report = probe.run(package)

    assert [failure.module for failure in report.failures] == ["probefixture_exits.quits"]
    assert report.failures[0].error_type == "SystemExit"


def test_discovery_excludes_a_directory_without_an_init(tmp_path: Path) -> None:
    """The exclusion rule is a fact about the tree, driven here directly.

    ``NOT_PACKAGE_MODULES`` asserts WHICH files today's rule excludes; this
    asserts the rule itself, against a tree built to have one of each. Without
    it, a rule that excluded everything would still satisfy an exact-set
    assertion the day someone regenerated the set from the broken rule.
    """
    package = _build_package(tmp_path, "probefixture_shape", {"real": "VALUE = 1\n"})
    nested = package.src_root / "sub"
    nested.mkdir()
    (nested / "__init__.py").write_text('"""Sub."""\n', encoding="utf-8")
    (nested / "deep.py").write_text("VALUE = 2\n", encoding="utf-8")
    loose = package.src_root / "examples"
    loose.mkdir()
    (loose / "script.py").write_text("VALUE = 3\n", encoding="utf-8")

    assert package.modules() == (
        "probefixture_shape",
        "probefixture_shape.real",
        "probefixture_shape.sub",
        "probefixture_shape.sub.deep",
    )
    assert package.skipped() == ("probefixture_shape.examples.script",)
