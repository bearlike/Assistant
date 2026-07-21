#!/usr/bin/env python3
"""Static proof that a ``guard.public()``/``guard.dual_channel()`` route's
handler actually CALLS the enforcement it declares.

Both decorators add zero request-time behaviour by design (see
``permission_guard.py``): the handler's own body is the enforcement, and the
decorator only records a claim about where it lives. Nothing in the request
path catches a handler that forgets to make the claim true, and
``guard.audit()`` cannot either — it partitions the live URL map by
DECLARATION, not by whether the declaration holds. This file closes that gap
with a static AST reachability check: for every such route, does the
handler's call graph actually reach the symbol its binding names?

**What is SOUND here, and what is not:**

- ``dual_channel`` carries a formal, non-empty, already-validated
  ``enforced_by`` field (``AlternateCredential.enforced_by`` in
  ``permission_guard.py``) naming exactly where the fall-through lives. This
  file turns that from a pointer an auditor reads into a reachability proof
  the suite checks on every run — see
  ``test_every_dual_channel_route_reaches_its_enforced_by_symbol``.
- ``public()`` carries no equivalent formal field — ``reason`` is prose, not
  a symbol, and several public routes are genuinely checkless by design (an
  OIDC login's entry point, a logout that must never itself 401). There is no
  sound way to derive "what to verify" from free text, so
  ``_PUBLIC_ROUTE_MANIFEST`` below is a ONE-TIME human classification of
  every current ``guard.public()`` call site, and the suite's job is only to
  keep that classification from going stale: a new, unclassified public
  route fails ``test_every_public_route_is_classified_in_the_manifest``
  rather than silently inheriting "probably fine", and a classified route
  whose claimed check stops being reachable fails
  ``test_every_classified_public_route_reaches_its_expected_check``.
- The reachability primitive matches by NAME (the final attribute of a call
  expression, or a bare name), not by resolved type — it cannot tell
  ``self.verify(...)`` from an unrelated ``other.verify(...)`` sharing that
  name. That means a false POSITIVE (crediting an unrelated same-named call)
  is possible in principle; a false NEGATIVE is not — a differently-named
  check can never match, so the check fails loudly rather than passing
  quietly on code it cannot follow. It also only hops one level into a
  ``self.<name>(...)`` delegation (bounded by ``max_depth``, default 2, which
  covers every shape observed on this branch); a handler that reaches its
  enforcement through anything else (a plain module function, dynamic
  dispatch, more than two hops) will show up as a failure here, not a
  silent miss — widen the search rather than special-case it if that happens.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# The reachability primitive
# ---------------------------------------------------------------------------


def _call_name(node: ast.expr) -> str | None:
    """The name a call expression would be matched by: ``f(...)`` -> ``f``;
    ``a.b.c(...)`` -> ``c`` (only the final attribute -- see module docstring
    on why matching by name, not by resolved receiver, is the accepted
    boundary here).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _body_call_nodes(func: Callable[..., Any]) -> list[ast.Call]:
    """Every ``Call`` node in *func*'s own BODY -- never its decorator list.

    Excluding decorators is deliberate: a ``dual_channel(..., enforced_by=
    "X.y")`` argument is a string literal, not a call, but a future decorator
    with a call-shaped argument must never be able to masquerade as
    enforcement the body doesn't actually perform.
    """
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return []
    tree = ast.parse(textwrap.dedent(source))
    funcdef = tree.body[0]
    if not isinstance(funcdef, ast.FunctionDef | ast.AsyncFunctionDef):
        return []
    calls: list[ast.Call] = []
    for stmt in funcdef.body:
        calls.extend(node for node in ast.walk(stmt) if isinstance(node, ast.Call))
    return calls


def _owning_class(func: Callable[..., Any]) -> type | None:
    """The class *func*'s qualname says it belongs to, or ``None`` for a bare
    module-level function (a flat ``Class.method`` qualname is exactly the
    shape every route handler in this app has -- see the route modules'
    ``_ControllerResource``/flask-restx ``Resource`` convention).
    """
    qualname = getattr(func, "__qualname__", "")
    if "." not in qualname:
        return None
    cls_name = qualname.rsplit(".", 1)[0]
    if "." in cls_name or "<locals>" in cls_name:
        return None  # nested/local -- not a route handler's shape here
    module = importlib.import_module(func.__module__)
    owner = getattr(module, cls_name, None)
    return owner if isinstance(owner, type) else None


def reaches_enforcement(
    func: Callable[..., Any],
    target_names: frozenset[str],
    *,
    max_depth: int = 2,
    _seen: set[tuple[str | None, str | None]] | None = None,
) -> bool:
    """Whether *func*'s call graph reaches a call named in *target_names*.

    Direct calls in *func*'s own body are checked first. If none match and
    *max_depth* allows, hops ONE level into any ``self.<name>(...)`` call by
    resolving ``<name>`` against *func*'s owning class (through its MRO, so
    an inherited helper like ``_ControllerResource._read_auth`` resolves from
    a subclass) and recursing into that method's body. This is what lets a
    route's own helper stand in for the controller method it delegates to,
    without the check needing to know every handler's exact internal shape.
    """
    if max_depth < 0:
        return False
    seen = _seen if _seen is not None else set()
    key = (getattr(func, "__module__", None), getattr(func, "__qualname__", None))
    if key in seen:
        return False
    seen.add(key)

    calls = _body_call_nodes(func)
    for node in calls:
        if _call_name(node.func) in target_names:
            return True

    if max_depth == 0:
        return False

    owning_cls = _owning_class(func)
    if owning_cls is None:
        return False
    for node in calls:
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)):
            continue
        if fn.value.id != "self":
            continue
        candidate = getattr(owning_cls, fn.attr, None)
        if callable(candidate) and reaches_enforcement(
            candidate, target_names, max_depth=max_depth - 1, _seen=seen
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Reachability primitive: proof it actually catches the defect class
# ---------------------------------------------------------------------------


def test_reachability_direct_call_is_found() -> None:
    def handler() -> None:
        _check()

    def _check() -> None:
        pass

    assert reaches_enforcement(handler, frozenset({"_check"})) is True


class _DummyOneHop:
    """Module-level on purpose: ``_owning_class`` resolves a handler's class
    via ``getattr(module, cls_name)``, which only works for a class the
    module can actually name -- real route handlers are always module-level
    classes, so a locally-nested test class would exercise a shape the
    checker deliberately refuses to resolve (see ``_owning_class``).
    """

    def handler(self) -> None:
        self._helper()

    def _helper(self) -> None:
        self._real_check()

    def _real_check(self) -> None:
        pass


def test_reachability_finds_a_one_hop_delegated_call() -> None:
    """Mirrors the real shape: handler -> self._helper() -> self._real_check()."""
    assert reaches_enforcement(_DummyOneHop.handler, frozenset({"_real_check"})) is True


def test_reachability_returns_false_when_never_called() -> None:
    """THE PROOF: a handler that claims a check but never makes it true is caught.

    This is exactly the shape ``public()``/``dual_channel()`` permit by
    design -- the decorator records a claim, nothing forces the handler to
    honour it. A checker that always passed such code would be worse than no
    checker at all (it would be trusted), so this pins that it does not.
    """

    class Dummy:
        def handler(self) -> None:
            """Docstring says 'verifies the signature'. The body does not."""
            return None

        def verify_signature(self) -> bool:
            return True

    assert reaches_enforcement(Dummy.handler, frozenset({"verify_signature"})) is False


def test_reachability_does_not_cross_into_a_sibling_method() -> None:
    """A same-named check on an UNRELATED class must not be found by accident."""

    class Elsewhere:
        def verify(self) -> bool:
            return True

    class Dummy:
        def handler(self) -> None:
            return None

    assert reaches_enforcement(Dummy.handler, frozenset({"verify"})) is False
    del Elsewhere  # defined only to prove it is NOT what gets matched above


# ---------------------------------------------------------------------------
# Handler resolution: binding key ("module.Qual.Name") -> the live callable
# ---------------------------------------------------------------------------


def _split_module_qualname(binding_key: str) -> tuple[str, str]:
    """Split a ``module.QualName`` binding key into its importable module and
    the dotted attribute path within it.

    A module name can itself contain dots (``mewbo_api.backend``), so this
    can't split on the first or last dot -- it tries the longest importable
    prefix, right-to-left, mirroring how ``pydoc.locate`` resolves a dotted
    path.
    """
    parts = binding_key.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        try:
            importlib.import_module(candidate)
        except ImportError:
            continue
        return candidate, ".".join(parts[i:])
    raise ImportError(f"no importable module prefix in {binding_key!r}")


def _resolve_handler(binding_key: str) -> Callable[..., Any]:
    module_name, qualname = _split_module_qualname(binding_key)
    obj: Any = importlib.import_module(module_name)
    for attr in qualname.split("."):
        obj = getattr(obj, attr)
    return obj


# ---------------------------------------------------------------------------
# dual_channel: sound, because enforced_by is a formal, validated field
# ---------------------------------------------------------------------------

# The vocabulary a SELF-referential ``enforced_by`` (the check lives in the
# decorated method's own body, not a delegate -- e.g. ``enforced_by=
# "ApiKeyRotate.post (owner check against list_keys_for_owner)"``) can point
# AT: there is nothing else to search for except this app's own primary-auth
# primitives. Grown by reading every self-referential ``enforced_by`` on this
# branch (``backend.py``'s three key routes); extend it, don't work around
# it, if a new one is added.
_SELF_ENFORCEMENT_VOCABULARY: frozenset[str] = frozenset(
    {
        "_require_master_token",
        "_require_permission",
        "current_principal",
        "_self_mint_denied",
        "require_api_key",
        "require_permission",
        "require_master_token",
    }
)


def _dual_channel_targets(handler_qualname: str, enforced_by: str) -> frozenset[str]:
    """What to search *handler_qualname*'s call graph for, derived from
    ``enforced_by``.

    ``enforced_by`` is free text of the shape ``ClassName.method_name`` or
    ``ClassName.method_name (parenthetical explanation)`` -- only the leading
    token is a real symbol reference (see ``AlternateCredential`` in
    ``permission_guard.py``). When that symbol names the decorated handler
    itself, the enforcement lives in the handler's own body; otherwise it
    names a delegate method the handler must actually call (directly, or one
    ``self.*`` hop away).
    """
    head = enforced_by.split("(", 1)[0].strip()
    if head == handler_qualname:
        return _SELF_ENFORCEMENT_VOCABULARY
    return frozenset({head.rsplit(".", 1)[-1]})


def _dual_channel_bindings() -> list[tuple[str, Any]]:
    from mewbo_api import backend  # noqa: F401  -- import registers every route
    from mewbo_api.auth.guard_registry import guard_registry

    return [
        (key, binding)
        for key, binding in guard_registry.guard.bindings.items()
        if binding.tier == "dual"
    ]


def test_every_dual_channel_route_reaches_its_enforced_by_symbol() -> None:
    """Turns ``enforced_by`` from a pointer an auditor reads into a proof.

    ``dual_channel()`` adds no request-time behaviour -- the handler's own
    body IS the enforcement, and ``enforced_by`` is the only formal record of
    where. This walks the handler's call graph and fails loudly if the named
    symbol is never actually invoked: exactly the class of bug a docstring
    claim cannot catch on its own.
    """
    bindings = _dual_channel_bindings()
    assert bindings, "no dual_channel routes found -- registration wiring broke"

    failures: list[str] = []
    for key, binding in bindings:
        assert binding.alternate is not None  # RouteBinding's tier=="dual" invariant
        handler = _resolve_handler(key)
        _, qualname = _split_module_qualname(key)
        targets = _dual_channel_targets(qualname, binding.alternate.enforced_by)
        if not reaches_enforcement(handler, targets):
            failures.append(
                f"{key}: enforced_by={binding.alternate.enforced_by!r} "
                f"target={sorted(targets)} not reached"
            )

    assert not failures, (
        "dual_channel route(s) whose enforced_by symbol is unreachable "
        "from the handler body:\n" + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# public(): human-classified manifest, kept honest by the suite
# ---------------------------------------------------------------------------

# See the module docstring for why this manifest exists instead of a purely
# mechanical derivation. Each value is either the name of the call that
# provides the compensating control the route's own `reason` claims, or
# `None` when the reason itself says no control is needed at all.
_PUBLIC_ROUTE_MANIFEST: dict[str, str | None] = {
    # -- genuinely open by design: nothing to check -------------------------
    # "the login screen calls this before any session exists" -- pure config read.
    "mewbo_api.iam.routes.IamRoutesController.authenticators": None,
    # "entry point of the browser login flow; no principal exists yet by definition"
    "mewbo_api.auth.routes.AuthRoutesController.login": None,
    "mewbo_api.auth.saml_routes.SamlRoutesController.login": None,
    # "clearing one's own cookie is idempotent and must never itself 401"
    "mewbo_api.auth.routes.AuthRoutesController.logout": None,
    # "SP metadata is published to identity-provider administrators by design"
    "mewbo_api.auth.saml_routes.SamlRoutesController.metadata": None,
    # -- a concrete call provides the proof the reason claims ---------------
    # "capability-URL webhook; the unguessable secret ... is the proof"
    "mewbo_api.triggers.routes.TriggerHook.post": "fire_hook",
    # "the adapter's own HMAC signature check is the proof" (webhook-capable
    # adapters only -- see channels/base.py's supports_webhook)
    "mewbo_api.channels.routes.webhook_receive": "verify_request",
    # "share links carry an unguessable token as their proof"
    "mewbo_api.backend.ShareLookup.get": "resolve",
    # "the password login form itself; it MINTS the credential a guard would demand"
    "mewbo_api.auth.routes.AuthRoutesController.login_with_password": "login_with_password",
    # "identity-provider redirect back; the signed state cookie is the proof"
    "mewbo_api.auth.routes.AuthRoutesController.callback": "complete_callback",
    # "identity-provider assertion POST-back; the signed assertion is the proof"
    "mewbo_api.auth.saml_routes.SamlRoutesController.acs": "complete_acs",
    # "identity probe; must be reachable anonymously to answer its own 401"
    "mewbo_api.auth.routes.AuthRoutesController.me": "current_principal",
}


def _public_bindings() -> list[tuple[str, Any]]:
    from mewbo_api import backend  # noqa: F401  -- import registers every route
    from mewbo_api.auth.guard_registry import guard_registry

    return [
        (key, binding)
        for key, binding in guard_registry.guard.bindings.items()
        if binding.tier == "public"
    ]


def test_every_public_route_is_classified_in_the_manifest() -> None:
    """A new ``guard.public()`` route must be classified, not left silent.

    This is the half of the coverage gap a reachability check alone can't
    close on its own: ``public()``'s ``reason`` is prose, so there is no
    formal field to derive an expectation from the way ``enforced_by`` lets
    ``dual_channel`` be checked automatically. Requiring a manifest entry is
    the deliberate trade -- a human reads the new route once, exactly as this
    manifest itself was built, and this assertion is what keeps that read
    from silently going stale as routes are added or removed.
    """
    live = {key for key, _ in _public_bindings()}
    manifest = set(_PUBLIC_ROUTE_MANIFEST)

    missing = live - manifest
    assert not missing, (
        "guard.public() route(s) with no manifest entry -- classify them in "
        "_PUBLIC_ROUTE_MANIFEST (the name of the call that backs the route's "
        f"own `reason`, or None if the reason says no check is needed): {sorted(missing)}"
    )
    stale = manifest - live
    assert not stale, f"manifest entries for routes that no longer exist: {sorted(stale)}"


def test_every_classified_public_route_reaches_its_expected_check() -> None:
    """For every manifest entry naming a check, verify it is actually reachable."""
    live = dict(_public_bindings())
    failures: list[str] = []
    for key, target in _PUBLIC_ROUTE_MANIFEST.items():
        if target is None:
            continue
        assert key in live, f"{key} is in the manifest but is not a live public route"
        handler = _resolve_handler(key)
        if not reaches_enforcement(handler, frozenset({target})):
            failures.append(f"{key}: expected a call to {target!r}, none found")

    assert not failures, (
        "public route(s) whose claimed compensating control is unreachable "
        "from the handler body:\n" + "\n".join(failures)
    )
