"""Architecture invariants for the Mewbo IAM layer.

These are NOT feature tests. Every assertion here protects a structural law that
was either designed in deliberately or restored after a real defect. A feature
test fails when behavior changes; these fail when the SHAPE of the system
changes — a new upward import, an eagerly-loaded optional driver, a variant that
skips the discriminated-union contract, a wire body that drifts.

The suite is deliberately small. Each test guards one law, and each docstring
states why the law exists and what breaks without it, because a reader who hits
a failure here needs the rationale more than the mechanics.

Everything is offline and deterministic: no MongoDB, no network, no live clock.
Where a fresh interpreter is genuinely required (an assertion about
``sys.modules``), a subprocess is used rather than a mock — see
``_run_probe``.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args, get_origin

import pytest
import tomllib
from flask import Flask, g, request
from mewbo_core.key_store import KeyScopes, KeyStore
from mewbo_iam import (
    ADMIN_ROLE,
    BUILTIN_ROLES,
    AccessDecider,
    AccessGrant,
    AuthAuditEvent,
    AuthAuditEventUnion,
    AuthenticatorSpec,
    AuthenticatorUnion,
    AuthMethod,
    AuthSettings,
    AvatarPolicy,
    Grantee,
    LoginFailureEvent,
    OidcAuthenticator,
    OwnershipStamp,
    PermissionCatalog,
    Principal,
    RoleRecord,
    TrustedHeaderAuthenticator,
    parse_audit_event,
    parse_authenticator,
)
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = REPO_ROOT / "packages" / "mewbo_core" / "src" / "mewbo_core"
IAM_SRC = REPO_ROOT / "packages" / "mewbo_iam" / "src" / "mewbo_iam"
IAM_PYPROJECT = REPO_ROOT / "packages" / "mewbo_iam" / "pyproject.toml"

# A fixed instant, injected wherever a timestamp is needed. Nothing in the
# kernel reads a clock, so no clock is ever patched in this file.
NOW = datetime(2026, 3, 4, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    """Every module name *path* imports, parsed from its AST.

    Parsing rather than grepping is load-bearing: both `mewbo_core` and
    `mewbo_iam` carry legitimate PROSE cross-references to the layers they must
    not import (the key-store docstring names ``mewbo_iam`` twice; the drivers
    package names ``mewbo_api``). A text search flags those and a reader then
    learns to ignore the test.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _upward_imports(root: Path, forbidden: tuple[str, ...]) -> list[str]:
    """Every ``file:line-ish`` offender importing one of *forbidden* under *root*."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for imported in sorted(_imported_modules(path)):
            top = imported.split(".", 1)[0]
            if top in forbidden:
                offenders.append(f"{path.relative_to(REPO_ROOT)} -> {imported}")
    return offenders


def _run_probe(code: str, *, cwd: Path | None = None, env: dict | None = None) -> dict:
    """Run *code* in a FRESH interpreter and return the JSON it prints.

    A `sys.modules` assertion made in-process is worthless: pytest has already
    imported half the world, so `"authlib" in sys.modules` is true regardless of
    what the code under test does. That is the exact trap that makes a naive
    version of the optional-dependency tests pass unconditionally.
    """
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


class _BlockedFinder:
    """A meta-path finder that makes named modules look uninstalled.

    Simulates a partial install (say, ``pip install mewbo-iam[ldap]`` with no
    OIDC extra) without touching the environment. Blocks both the
    ``importlib.util.find_spec`` probe and a plain ``__import__``, which is what
    the two probe styles in the codebase actually use.
    """

    def __init__(self, blocked: tuple[str, ...]) -> None:
        """Capture the module prefixes to hide."""
        self._blocked = blocked

    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001, ANN201
        """Raise for a blocked module so it reads as absent, else defer."""
        for name in self._blocked:
            if fullname == name or fullname.startswith(f"{name}."):
                raise ImportError(f"simulated missing dependency: {fullname}")
        return None


@pytest.fixture()
def hide_modules(monkeypatch: pytest.MonkeyPatch):
    """Return a callable that hides top-level modules for the test's duration."""

    def _hide(*names: str) -> None:
        for cached in [m for m in list(sys.modules) if m.split(".", 1)[0] in names]:
            monkeypatch.delitem(sys.modules, cached, raising=False)
        finder = _BlockedFinder(names)
        monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])

    return _hide


def _disabled_kit(tmp_path: Path):
    """A default (auth-disabled) AuthKit over a real JSON key store."""
    from mewbo_api.auth import AuthKit

    return AuthKit(
        settings=AuthSettings(),
        key_store=(lambda store=KeyStore(path=str(tmp_path / "keys.json")): store),
        credential_reader=lambda *_a, **_kw: None,
        master_matcher=lambda _token: False,
    )


def _enabled_kit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    credential_reader=lambda *_a, **_kw: None,
):
    """An auth-ENABLED AuthKit whose lazily-built IAM stores land in *tmp_path*.

    The kit's real store factories are redirected (not faked) at the one I/O
    boundary, so the guard paths below run the genuine role-resolution and audit
    code against temp files instead of the deployment's config directory.
    """
    from mewbo_api.auth import kit as kit_module
    from mewbo_iam import create_auth_audit_store, create_role_store

    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        kit_module, "create_role_store", lambda: create_role_store(tmp_path / "iam_roles.json")
    )
    monkeypatch.setattr(
        kit_module,
        "create_auth_audit_store",
        lambda: create_auth_audit_store(tmp_path / "iam_audit.json"),
    )
    return kit_module.AuthKit(
        settings=AuthSettings(enabled=True),
        key_store=(lambda store=KeyStore(path=str(tmp_path / "keys.json")): store),
        credential_reader=credential_reader,
        master_matcher=lambda _token: False,
    )


# ---------------------------------------------------------------------------
# A. Layering — the dependency DAG
# ---------------------------------------------------------------------------


def test_core_never_imports_the_identity_kernel() -> None:
    """`mewbo_core` is the base of the DAG and may not import `mewbo_iam`.

    Core is the lean SDK every interface (CLI, API, Home Assistant, MCP) links
    against. If it reached up into the identity kernel, a CLI-only install would
    start requiring the IAM package, and the two would become a dependency
    cycle the moment the kernel needed anything from core — which it does (the
    stores use core's config/logging seam). The direction is what keeps the
    relationship acyclic.

    The seam that makes this possible is `KeyScopes`, which lives in core as the
    pure three-state matcher the kernel imports DOWN into.
    """
    offenders = _upward_imports(CORE_SRC, ("mewbo_iam",))
    assert offenders == [], f"mewbo_core imports the identity kernel: {offenders}"


def test_identity_kernel_never_imports_upward() -> None:
    """`mewbo_iam` is a capability library — it imports core, never sideways or up.

    An import of `mewbo_api` would make the kernel unusable outside the HTTP app
    (the CLI and MCP surfaces resolve principals too); an import of
    `mewbo_tools`/`mewbo_graph` would drag heavy subprocess/graph dependencies
    into a package whose whole value is being dependency-light. Apps consume it;
    it consumes no app.
    """
    offenders = _upward_imports(IAM_SRC, ("mewbo_api", "mewbo_tools", "mewbo_graph"))
    assert offenders == [], f"identity kernel imports upward: {offenders}"

    # Non-vacuity: the same scanner must SEE the kernel's legitimate downward
    # import of core, otherwise an empty result above would prove nothing.
    assert _upward_imports(IAM_SRC, ("mewbo_core",)), "the import scanner found nothing at all"


def test_identity_kernel_declares_only_core_and_pydantic_as_base_deps() -> None:
    """A bare `mewbo-iam` install pulls in nothing but mewbo-core and pydantic.

    The import graph above only proves nothing is imported TODAY. This proves the
    package cannot quietly acquire a heavy runtime dependency: every provider
    integration (authlib, joserfc, ldap3, python3-saml) must stay behind an
    extra, so an operator who runs local API keys never installs a SAML XML
    stack. A new base dependency here is a deliberate decision, not a drive-by.
    """
    manifest = tomllib.loads(IAM_PYPROJECT.read_text(encoding="utf-8"))
    declared = {
        dep.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip().lower()
        for dep in manifest["project"]["dependencies"]
    }
    assert declared == {"mewbo-core", "pydantic"}, f"unexpected base dependencies: {declared}"
    # And the heavy drivers really are behind extras, not merely absent above.
    assert set(manifest["project"]["optional-dependencies"]) == {"oidc", "ldap", "saml"}


# ---------------------------------------------------------------------------
# B. Optional-dependency independence
# ---------------------------------------------------------------------------


def test_importing_the_kernel_loads_no_driver_dependency() -> None:
    """`import mewbo_iam` must not drag in authlib, joserfc, ldap3, or python3-saml.

    The kernel's models are pure — claims and directory entries arrive as
    mappings — so a deployment that only stores users and roles pays nothing for
    the providers it does not use. If a driver leaked into the package body, a
    bare install would fail at import with a `ModuleNotFoundError` naming a
    third-party package the operator never asked for.

    Runs in a subprocess because this process already has every extra installed
    and imported; asserting on the parent's `sys.modules` would pass no matter
    what the package does.
    """
    loaded = _run_probe(
        "import json, sys; import mewbo_iam; "
        "print(json.dumps({"
        "'drivers': sorted(m for m in ('authlib','joserfc','ldap3','onelogin') "
        "if m in sys.modules), 'kernel': 'mewbo_iam' in sys.modules}))"
    )
    # Non-vacuity: the probe really did import the kernel, so an empty driver
    # list is a fact about the package rather than about a broken probe.
    assert loaded["kernel"] is True
    assert loaded["drivers"] == [], (
        f"importing mewbo_iam eagerly loaded driver modules: {loaded['drivers']}"
    )


@pytest.mark.parametrize(
    ("hidden", "hidden_extra", "hidden_name", "surviving_name"),
    [
        (("authlib", "joserfc"), "oidc", "DiscoveryCache", "LdapBinder"),
        (("ldap3",), "ldap", "LdapBinder", "OidcClient"),
    ],
    ids=["oidc-extra-missing", "ldap-extra-missing"],
)
def test_each_driver_extra_resolves_independently(
    hide_modules, hidden: tuple[str, ...], hidden_extra: str, hidden_name: str, surviving_name: str
) -> None:
    """An LDAP-only install must reach `LdapBinder` without the OIDC extra, and vice versa.

    This is a REGRESSION test for a shipped defect. `mewbo_iam.drivers` used to
    import every driver in its module body behind ONE dependency probe, so an
    operator who installed `mewbo-iam[ldap]` and configured only a directory got
    `LdapBinder requires the 'oidc' extra` — a name they had no reason to
    install. The cure was per-name lazy resolution (PEP 562 `__getattr__`), and
    this test is what stops a future refactor from collapsing it back into one
    eager body.

    Each extra is hidden in a SEPARATE case (hiding both in one test body would
    leave the first case's block in force and make the second assert nothing),
    then BOTH directions are checked: the other extra still resolves, and the
    hidden one raises an ImportError naming its OWN extra — the actionable half,
    since an operator must learn which extra to install, not merely that
    something is missing.
    """
    hide_modules(*hidden)
    drivers = importlib.import_module("mewbo_iam.drivers")

    assert getattr(drivers, surviving_name) is not None, (
        f"{surviving_name} became unreachable when the '{hidden_extra}' extra "
        "was missing — the extras are coupled again"
    )
    with pytest.raises(ImportError) as excinfo:
        getattr(drivers, hidden_name)
    assert f"'{hidden_extra}' extra" in str(excinfo.value), (
        f"{hidden_name} must name its own extra; got: {excinfo.value}"
    )


def test_importing_the_stores_does_not_import_pymongo() -> None:
    """`mewbo_iam.stores` loads the JSON drivers only; Mongo arrives lazily or never.

    A JSON-driver deployment must never need a reachable MongoDB, and `pymongo`
    is imported ONLY inside `stores/mongo.py`, reached from each `create_*_store`
    factory when `storage.driver == "mongodb"`. Importing it at the package level
    would make every install carry the driver and would move a config decision to
    import time, where no config has been read yet.
    """
    loaded = _run_probe(
        "import json, sys; import mewbo_iam.stores; "
        "print(json.dumps('pymongo' in sys.modules))"
    )
    assert loaded is False, "importing mewbo_iam.stores pulled in pymongo"


# ---------------------------------------------------------------------------
# C. Runtime independence — auth disabled is the default
# ---------------------------------------------------------------------------


def test_disabled_auth_writes_no_iam_store_file(tmp_path: Path) -> None:
    """A default deployment must leave zero `iam_*.json` files on disk.

    "Auth is off" has to mean the identity layer is genuinely inert, not merely
    permissive. A store created eagerly at boot would write role/user/audit files
    into every existing deployment's data directory on upgrade — visible,
    surprising, and a support burden for operators who never turned IAM on. The
    AuthKit therefore builds its stores lazily and the route mounts return early.

    Runs in a subprocess with its own `MEWBO_HOME` and a config-free CWD so the
    store paths genuinely resolve into the temp directory — no patched path
    helper, which would test the patch instead of the behavior.
    """

    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "MEWBO_HOME": str(home)}
    env.pop("MEWBO_CONFIG", None)

    probe = """
import json, os
from pathlib import Path
from flask import Flask, g, request
from mewbo_core.key_store import KeyStore
from mewbo_iam import AuthSettings
from mewbo_api.auth import AuthKit
from mewbo_api.iam import init_iam_routes
from mewbo_api.scim import init_scim

home = Path(os.environ["MEWBO_HOME"])
kit = AuthKit(
    settings=AuthSettings(),
    key_store=(lambda store=KeyStore(path=str(home / "keys.json")): store),
    credential_reader=lambda *a, **k: None,
    master_matcher=lambda t: False,
)
app = Flask(__name__)


@app.before_request
def _resolve():
    g.principal = kit.resolve(request)


guard = kit.require_permission("wiki.read")


@app.get("/probe")
def probe():
    return {"denied": guard()}


iam = init_iam_routes(
    app,
    settings=kit.settings,
)
scim = init_scim(app, settings=kit.settings, deprovision=lambda s: None)
client = app.test_client()
status = client.get("/probe").status_code
after_request = sorted(p.name for p in home.rglob("iam_*.json"))

# Non-vacuity: prove this glob is watching the directory the stores WOULD use,
# by deliberately building one and confirming it lands here.
from mewbo_iam import create_role_store

create_role_store().list()
after_deliberate_store = sorted(p.name for p in home.rglob("iam_*.json"))

print(json.dumps({
    "status": status,
    "iam_mounted": iam is not None,
    "scim_mounted": scim is not None,
    "iam_files": after_request,
    "observable": after_deliberate_store,
}))
"""
    result = _run_probe(probe, cwd=tmp_path, env=env)
    assert result["status"] == 200
    assert result["iam_files"] == [], f"disabled deployment wrote IAM stores: {result['iam_files']}"
    assert result["iam_mounted"] is False
    assert result["scim_mounted"] is False
    assert "iam_roles.json" in result["observable"], (
        "the probe is not watching the directory the IAM stores resolve to, so the "
        "empty result above would have proved nothing"
    )


def test_disabled_auth_builds_no_provider_runtime(tmp_path: Path) -> None:
    """With auth off, the OIDC/SAML/federated runtimes stay `None` — never built.

    Each runtime owns a network client and a store (JWKS caches, the user store,
    cookie signers). Building one "just in case" would open the door to a
    discovery fetch or a store write on a deployment that configured nothing, and
    would make the disabled path depend on the optional extras being installed.
    `None` is the contract every consumer branches on, so it must be reachable
    without any of that machinery existing.
    """
    kit = _disabled_kit(tmp_path)

    assert kit.enabled is False
    assert kit.federated_runtime is None
    assert kit.oidc_runtime is None
    assert kit.saml_runtime is None
    assert kit.ldap_login is None
    assert kit.password_login_enabled is False


def test_disabled_auth_hides_iam_and_scim_but_keeps_auth_me(tmp_path: Path) -> None:
    """IAM/SCIM are unreachable when auth is off; `/api/auth/me` stays reachable.

    Two halves of one contract, and they pull in opposite directions on purpose.
    The admin and provisioning surfaces must not exist at all — a mounted route
    that answers 403 still advertises that the surface is there and still needs a
    store behind it. `/api/auth/me` is the exception because the console calls it
    unconditionally to decide whether to render a login affordance: it must
    answer 200 with `auth_enabled: false`, so a default deployment renders as a
    signed-in full-access user rather than showing a login screen nothing can
    satisfy.
    """
    from mewbo_api.auth import AuthRoutesController, init_auth_routes
    from mewbo_api.iam import init_iam_routes
    from mewbo_api.scim import init_scim

    kit = _disabled_kit(tmp_path)
    app = Flask(__name__)

    @app.before_request
    def _resolve() -> None:
        g.principal = kit.resolve(request)

    init_auth_routes(app, AuthRoutesController.from_kit(kit))
    init_iam_routes(
        app,
        settings=kit.settings,
    )
    init_scim(app, settings=kit.settings, deprovision=lambda _s: None)
    client = app.test_client()

    assert client.get("/api/iam/users").status_code == 404
    assert client.get("/api/iam/roles").status_code == 404
    assert client.get("/api/scim/v2/Users").status_code == 404

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    body = me.get_json()
    assert body["auth_enabled"] is False
    assert body["authenticated"] is True
    assert ADMIN_ROLE in body["roles"]


def test_permission_guard_passes_without_a_principal_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With auth off, a permission guard passes even where no principal was resolved.

    This is a REGRESSION test for a shipped defect. The guard read `g.principal`
    first, so a blueprint mounted on a bare Flask app — which the wiki route
    tests do, and which any surface composed outside the main app does — found no
    principal and rejected EVERY request. A deployment with auth switched OFF
    behaved as a fully locked one.

    The cure is ordering: "auth off ⇒ every permission passes" is a property of
    the CONFIGURATION, so the disabled check runs before any request state is
    read. Note the deliberate asymmetry asserted below — with auth ENABLED an
    absent principal still fails closed with 401, because a surface that enforces
    permissions without resolving identity must not be permissive.
    """
    disabled = _disabled_kit(tmp_path)
    app = Flask(__name__)  # deliberately NO before_request resolver

    with app.test_request_context("/anything"):
        assert disabled.require_permission(PermissionCatalog.WIKI_READ)() is None
        assert disabled.require_permission(PermissionCatalog.USERS_ADMIN)() is None

        enabled = _enabled_kit(tmp_path / "enabled", monkeypatch)
        assert enabled.require_permission(PermissionCatalog.WIKI_READ)() == (
            {"message": "Unauthorized"},
            401,
        )


# ---------------------------------------------------------------------------
# D. Kernel cohesion — the model laws
# ---------------------------------------------------------------------------


def test_scopes_three_states_survive_the_key_store_round_trip(tmp_path: Path) -> None:
    """`None` / `()` / non-empty are three distinct scope states and never collapse.

    `None` means unrestricted-legacy, `()` means a service key was granted
    exactly nothing, and a non-empty tuple means those scopes precisely.
    Collapsing `None` and `()` is the classic fail-open bug: a deliberately
    scopeless service key silently becomes omnipotent.

    Asserted at both ends — on `KeyScopes.matches`, the one matcher every caller
    shares, and through a real create -> resolve -> rotate cycle on the JSON
    driver, because a rotation that defaults an absent field to `None` (or a
    present empty list to absent) is precisely how the collapse would sneak back
    in without any matcher change.
    """
    assert KeyScopes(None).matches("sessions.read") is True
    assert KeyScopes(()).matches("sessions.read") is False
    assert KeyScopes(("sessions.read",)).matches("sessions.read") is True
    assert KeyScopes(("sessions.read",)).matches("wiki.read") is False
    assert KeyScopes(None).unrestricted is True
    assert KeyScopes(()).unrestricted is False

    store = KeyStore(path=str(tmp_path / "keys.json"))

    # (a) explicitly none — created [], resolved [], and still [] after rotation.
    secret, scopeless = store.create_scoped_key("scopeless", owner_subject="svc:a", scopes=[])
    assert scopeless["scopes"] == []
    assert store.resolve_key(secret)["scopes"] == []
    rotated_secret, rotated_scopeless = store.rotate_key(scopeless["id"])
    assert rotated_scopeless["scopes"] == [], "an explicitly-scopeless key widened on rotation"
    assert store.resolve_key(rotated_secret)["scopes"] == []
    assert KeyScopes.from_record(rotated_scopeless).unrestricted is False
    assert KeyScopes.from_record(rotated_scopeless).matches("sessions.read") is False

    # (b) unrestricted-legacy — the field is ABSENT, and stays absent.
    legacy_secret, legacy = store.create_key("legacy")
    assert "scopes" not in legacy
    assert "scopes" not in store.resolve_key(legacy_secret)
    rotated_legacy_secret, rotated_legacy = store.rotate_key(legacy["id"])
    assert "scopes" not in rotated_legacy, "a legacy key gained a scopes field on rotation"
    assert "scopes" not in store.resolve_key(rotated_legacy_secret)
    assert KeyScopes.from_record(rotated_legacy).unrestricted is True

    # (c) exact — carried through verbatim.
    narrow_secret, narrow = store.create_scoped_key("narrow", scopes=["sessions.read"])
    assert store.resolve_key(narrow_secret)["scopes"] == ["sessions.read"]
    _, rotated_narrow = store.rotate_key(narrow["id"])
    assert rotated_narrow["scopes"] == ["sessions.read"]

    # The principal carries the same three states, distinctly.
    method = AuthMethod(kind="api_key")
    unrestricted = Principal(subject="svc:a", kind="service", scopes=None, auth_method=method)
    explicit_none = Principal(subject="svc:b", kind="service", scopes=(), auth_method=method)
    assert unrestricted.scopes is None
    assert explicit_none.scopes == ()


@pytest.mark.parametrize(
    ("base", "union", "discriminator", "parse_seam"),
    [
        (AuthenticatorSpec, AuthenticatorUnion, "kind", parse_authenticator),
        (AuthAuditEvent, AuthAuditEventUnion, "type", parse_audit_event),
    ],
)
def test_variant_families_are_discriminated_unions_with_one_parse_seam(
    base, union, discriminator: str, parse_seam
) -> None:
    """Every variant family stays data-owned: no base discriminator, one parse seam.

    A family of variants must never be dispatched by a service-side
    `if kind == ...` switch, because such a switch drifts out of sync the moment
    a variant gains a field, and the drift is silent. The structural conditions
    that make the switch unnecessary are asserted here:

    * the base declares NO discriminator field — a mutable `kind: str` on the
      base that each variant narrows to a `Literal` is an incompatible field
      override, so the discriminator lives only on the members;
    * every concrete variant declares its OWN single-valued `Literal`, in its own
      class body rather than inherited;
    * the union's members are exactly the concrete subclasses — a new variant
      that is written but never wired into the union fails HERE rather than
      parsing into the wrong type at runtime;
    * the public parse function is the same object as the class-owned seam, so
      there is one adapter and not two that can disagree.
    """
    assert discriminator not in base.model_fields, (
        f"{base.__name__} declares the '{discriminator}' discriminator on the base; "
        "it belongs only on the concrete variants"
    )
    # Compared by underlying function, not by identity: ``parse`` is a
    # classmethod, so every attribute access mints a fresh bound object and an
    # ``is`` check could never hold even for the correct wiring.
    assert parse_seam.__func__ is base.parse.__func__, (
        "the module-level parse function is not the class-owned seam — two "
        "adapters exist and can disagree"
    )
    assert parse_seam.__self__ is base

    concrete: set[type] = set()
    pending = list(base.__subclasses__())
    while pending:
        cls = pending.pop()
        concrete.add(cls)
        pending.extend(cls.__subclasses__())

    members = set(get_args(get_args(union)[0]))
    # Non-vacuity: two empty sets would compare equal and assert nothing.
    assert len(concrete) >= 3, f"{base.__name__} resolved suspiciously few variants: {concrete}"
    assert members == concrete, (
        f"union members {sorted(c.__name__ for c in members)} != concrete subclasses "
        f"{sorted(c.__name__ for c in concrete)}"
    )

    for variant in sorted(concrete, key=lambda c: c.__name__):
        annotations = vars(variant).get("__annotations__", {})
        assert discriminator in annotations, (
            f"{variant.__name__} inherits its discriminator instead of declaring it"
        )
        annotation = variant.model_fields[discriminator].annotation
        assert get_origin(annotation) is Literal, (
            f"{variant.__name__}.{discriminator} must be a Literal, got {annotation}"
        )
        assert len(get_args(annotation)) == 1, (
            f"{variant.__name__}.{discriminator} must name exactly one value"
        )


def test_permission_catalog_is_closed_and_derived() -> None:
    """`ALL` is derived from the declared constants, and no role may escape it.

    The catalog being closed is what makes authorization auditable: a permission
    is a narrow id declared once, so a typo at a call site is a loud failure
    rather than a grant that can never be satisfied by any role. Two things would
    break that — a hand-maintained second list of ids (which drifts the first
    time someone adds a constant and forgets the list), and a role naming a
    permission outside the catalog (a silently ineffective grant that reads like
    a real one in the admin UI).

    `ALL` is therefore recomputed here from the class body independently of the
    derivation the module performs, so the two disagree loudly if the assembly
    is ever replaced by a literal.
    """
    derived = {
        value
        for name, value in vars(PermissionCatalog).items()
        if name.isupper() and name != "ALL" and isinstance(value, str)
    }
    assert derived, "the catalog declares no permission constants"
    assert PermissionCatalog.ALL == derived, "PermissionCatalog.ALL drifted from its constants"
    assert all("." in perm for perm in derived), "every id is '<domain>.<verb>'"

    for role in BUILTIN_ROLES:
        assert role.permissions <= PermissionCatalog.ALL, (
            f"built-in role {role.name!r} grants permissions outside the catalog: "
            f"{sorted(role.permissions - PermissionCatalog.ALL)}"
        )

    with pytest.raises(ValidationError):
        RoleRecord(name="rogue", permissions=frozenset({"sessions.teleport"}))

    assert PermissionCatalog.is_valid(PermissionCatalog.WIKI_READ) is True
    assert PermissionCatalog.is_valid("sessions.teleport") is False


def test_kernel_models_decide_from_injected_data_only() -> None:
    """Every kernel decision is a pure function of its arguments — no I/O, no clock.

    Behavior intrinsic to the data lives ON the model, but the inputs arrive as
    METHOD ARGUMENTS: HTTP headers, IdP claims, resolved role records, the
    ownership stamp, the avatar policy, the timestamp. A model that fetched its
    own inputs would be untestable without a network and would drag transport
    concerns into the layer whose value is being transport-free.

    **The absence of any patching in this test IS the assertion.** Every call
    below runs against plain data with a fixed `NOW`; if a future change made a
    model reach for a clock or a socket, restoring this test would require
    monkeypatching, and that requirement is the signal.

    The structural half is asserted alongside it: the pure model modules import
    no I/O library and contain no clock read, so the property cannot regress
    quietly between behavioral assertions.
    """
    header_auth = TrustedHeaderAuthenticator(name="proxy", trusted_proxies=("10.0.0.0/8",))
    identity = header_auth.resolve(
        {
            "X-Forwarded-User": "alice",
            "X-Forwarded-Email": "Alice@Example.com ",
            "X-Forwarded-Groups": "eng,platform",
        }
    )
    assert identity is not None
    assert identity.external_subject.subject == "alice"
    assert identity.groups == ("eng", "platform")

    oidc = OidcAuthenticator(
        name="idp",
        issuer="https://idp.example.com",
        discovery_url="https://idp.example.com/.well-known/openid-configuration",
        client_id="mewbo",
        client_secret="s3cret",
        groups_claim="resource_access.mewbo.roles",
    )
    claims = {
        "sub": "u-1",
        "email": "u@example.com",
        "resource_access": {"mewbo": {"roles": ["a"]}},
    }
    resolved = oidc.resolve(claims)
    assert resolved is not None and resolved.groups == ("a",)
    assert oidc.resolve({"email": "u@example.com"}) is None

    member = RoleRecord(name="member", permissions=frozenset({PermissionCatalog.WIKI_READ}))
    principal = Principal(
        subject="user:1",
        kind="user",
        roles=("member", "deleted-role"),
        email="Alice@Example.com",
        auth_method=AuthMethod(kind="oidc", issuer="https://idp.example.com"),
    )
    assert principal.effective_permissions({"member": member}) == frozenset({"wiki.read"})

    assert principal.avatar_url(AvatarPolicy(gravatar_enabled=False)) is None
    assert principal.avatar_url(AvatarPolicy(gravatar_enabled=True)).startswith(
        "https://www.gravatar.com/avatar/"
    )

    decider = AccessDecider()
    stamp = OwnershipStamp(owner_subject="user:2")
    grant = AccessGrant(
        resource_kind="session",
        resource_id="s-1",
        grantee=Grantee(kind="user", id="user:1"),
        level="write",
    )
    assert decider.decide(principal, stamp, [grant], "read") is True
    assert decider.decide(principal, stamp, [grant], "admin") is False
    assert decider.decide(principal, stamp, [], "read") is False

    # A timestamp is data the caller supplies; nothing here reads a clock.
    event = LoginFailureEvent(ts=NOW, source="api", method="api_key", reason="invalid_key")
    assert event.ts == NOW
    assert parse_audit_event(event.model_dump()) == event

    _assert_pure_model_modules()


def _assert_pure_model_modules() -> None:
    """Structural half of the purity law: no I/O imports, no clock reads."""
    banned_imports = {
        "requests",
        "httpx",
        "urllib",
        "socket",
        "pymongo",
        "ldap3",
        "authlib",
        "joserfc",
        "onelogin",
        "flask",
        "os",
        "pathlib",
    }
    clock_calls = {"now", "utcnow", "today", "time", "monotonic"}
    model_modules = (
        "principal.py",
        "authenticators.py",
        "permissions.py",
        "roles.py",
        "teams.py",
        "access.py",
        "mappings.py",
        "users.py",
        "audit.py",
        "settings.py",
        "scim.py",
    )
    for name in model_modules:
        path = IAM_SRC / name
        imports = _imported_modules(path)
        imported = {module.split(".", 1)[0] for module in imports}
        leaked = imported & banned_imports
        assert not leaked, f"{name} imports I/O: {sorted(leaked)}"

        # The drivers are the network+crypto half of the same package. They
        # import the models; a model importing a driver would invert that and
        # drag an optional extra into the dependency-light core.
        drivers = sorted(m for m in imports if m.startswith("mewbo_iam.drivers"))
        assert not drivers, f"{name} imports a driver: {drivers}"

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in clock_calls, (
                    f"{name}:{node.lineno} reads a clock ({node.func.attr}); "
                    "a timestamp must arrive as a method argument"
                )


def test_trusted_header_authenticator_cannot_exist_without_an_allowlist() -> None:
    """A proxy-header authenticator requires a non-empty, valid `trusted_proxies`.

    Identity headers are trivially forgeable by anyone who can reach the server
    directly, so they are only meaningful when a trusted proxy is the sole
    ingress and strips them from client input. An authenticator constructed with
    an empty allowlist would accept `X-Forwarded-User: admin` from any caller —
    a complete authentication bypass, not a degraded mode.

    Validation therefore happens AT DEFINITION: the unsafe object cannot be
    constructed at all, so no call site has to remember to check. `matches` is
    asserted alongside it because an allowlist that admits everything is the same
    bypass wearing a valid shape.
    """
    with pytest.raises(ValidationError):
        TrustedHeaderAuthenticator(name="proxy", trusted_proxies=())
    with pytest.raises(ValidationError):
        TrustedHeaderAuthenticator(name="proxy")
    with pytest.raises(ValidationError):
        TrustedHeaderAuthenticator(name="proxy", trusted_proxies=("not-a-cidr",))

    auth = TrustedHeaderAuthenticator(name="proxy", trusted_proxies=("192.168.1.0/24",))
    assert auth.is_trusted_source("192.168.1.7") is True
    assert auth.is_trusted_source("10.0.0.7") is False
    assert auth.is_trusted_source("") is False
    assert auth.is_trusted_source("not-an-address") is False

    # Parsing through the one seam applies the same validator.
    with pytest.raises(ValidationError):
        parse_authenticator({"kind": "trusted_header", "name": "p", "trusted_proxies": []})


# ---------------------------------------------------------------------------
# E. Wire-contract compatibility
# ---------------------------------------------------------------------------


def test_legacy_auth_bodies_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The typed error taxonomy reproduces the shipped auth bodies exactly.

    Introducing typed errors was a refactor of HOW a refusal is produced, never
    of WHAT a client receives. Roughly ninety call sites, the console, the MCP
    facade and the CLI all match these bodies; the 410 envelope in particular is
    matched byte-for-byte by the console's `session_terminated` sentinel. A
    "tidier" reason string here is a silent client-side regression with no
    compile-time signal anywhere.

    Both ends are asserted: the typed classes render the exact bodies, and the
    live `AuthKit` guards — the code paths that actually answer a request —
    return the same dicts, so the two definitions cannot drift apart.
    """
    from mewbo_api.errors import AuthenticationRequired, PermissionDenied, SessionTerminated
    from mewbo_api.responses import ApiResponseKit

    assert AuthenticationRequired.missing_credential().response() == (
        {"message": "API token is not provided."},
        401,
    )
    assert AuthenticationRequired.invalid_credential().response() == (
        {"message": "Unauthorized"},
        401,
    )
    assert PermissionDenied.insufficient_role(PermissionCatalog.WIKI_READ).response() == (
        {"message": "insufficient role"},
        403,
    )
    assert SessionTerminated().response() == (
        {
            "error": {
                "code": "session_terminated",
                "reason": "Session is permanently terminated",
                "retryable": False,
            }
        },
        410,
    )
    assert SessionTerminated().response() == ApiResponseKit.terminated_response()

    # The live guards must produce the same bodies as the typed classes.
    presented: list[str | None] = [None]
    kit = _enabled_kit(
        tmp_path, monkeypatch, credential_reader=lambda *_a, **_kw: presented[0]
    )
    assert kit.require_api_key() == ({"message": "API token is not provided."}, 401)
    assert kit.require_master_token() == ({"message": "API token is not provided."}, 401)

    presented[0] = "mk_wrong"
    assert kit.require_api_key() == ({"message": "Unauthorized"}, 401)
    assert kit.require_master_token() == ({"message": "Unauthorized"}, 401)

    app = Flask(__name__)
    denied_principal = Principal(
        subject="user:1",
        kind="user",
        roles=("viewer",),
        auth_method=AuthMethod(kind="oidc", issuer="https://idp.example.com"),
    )
    with app.test_request_context("/anything"):
        g.principal = denied_principal
        assert kit.require_permission(PermissionCatalog.USERS_ADMIN)() == (
            {"message": "insufficient role"},
            403,
        )
