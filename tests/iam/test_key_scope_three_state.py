#!/usr/bin/env python3
"""``scopes`` is THREE states, and collapsing any two of them fails open.

* ``None`` — unrestricted-legacy: impose no narrowing.
* ``()``/``[]`` — explicitly none: a scopeless service key can do nothing
  scope-gated.
* a non-empty collection — exactly those.

The dangerous collapse is ``()`` → ``None``: a key that was deliberately granted
zero scopes silently becoming unrestricted. These drive the law through
``KeyScopes.matches`` AND round-trip it through the key store's
create → resolve → **rotate** cycle on the JSON driver, because a coercion
introduced during persistence or rotation is exactly as fail-open as one in the
matcher.
"""

from __future__ import annotations

from mewbo_core.key_store import KeyScopes, KeyStore

# ── the matcher: the three states ─────────────────────────────────────────────


def test_none_is_unrestricted_and_matches_anything():
    """The legacy state imposes no narrowing at all."""
    scopes = KeyScopes(None)

    assert scopes.unrestricted is True
    assert scopes.granted is None
    assert scopes.matches("sessions.read") is True
    assert scopes.matches("anything.at.all") is True


def test_an_empty_tuple_is_explicitly_none_and_matches_nothing():
    """The fail-open bug this law exists to prevent: ``()`` is NOT ``None``."""
    scopes = KeyScopes(())

    assert scopes.unrestricted is False, "an explicitly-scopeless key is not unrestricted"
    assert scopes.granted == ()
    assert scopes.matches("sessions.read") is False
    assert scopes.matches("anything.at.all") is False


def test_an_empty_list_is_the_same_explicitly_none_state():
    """A stored ``[]`` reads back as explicitly-none, not as legacy."""
    scopes = KeyScopes([])

    assert scopes.unrestricted is False
    assert scopes.matches("sessions.read") is False


def test_a_non_empty_scope_set_matches_exactly_what_it_grants():
    """The third state is an allowlist, checked by exact id."""
    scopes = KeyScopes(["sessions.read", "wiki.read"])

    assert scopes.unrestricted is False
    assert scopes.matches("sessions.read") is True
    assert scopes.matches("wiki.read") is True
    assert scopes.matches("sessions.write") is False
    assert scopes.matches("keys.admin") is False


# ── the wildcard form, and the bare "*" that deliberately is not one ──────────
def test_a_family_wildcard_matches_everything_under_that_family():
    """``sessions.*`` narrows to one family — that is the only wildcard form."""
    scopes = KeyScopes(["sessions.*"])

    assert scopes.matches("sessions.read") is True
    assert scopes.matches("sessions.write") is True
    assert scopes.matches("sessions.admin.export") is True


def test_a_family_wildcard_does_not_leak_into_a_similarly_named_family():
    """Prefix matching is through the dot — ``sessionsx`` is a different family."""
    scopes = KeyScopes(["sessions.*"])

    assert scopes.matches("sessionsx.read") is False
    assert scopes.matches("wiki.read") is False


def test_a_bare_star_scope_matches_NOTHING():
    """"Unrestricted" is exclusively ``None``'s job and must never be re-derivable.

    A bare ``"*"`` is deliberately not a wildcard: if it were, any path that can
    write a scope list could mint an unrestricted key without ever passing
    ``None``, and the three-state law would have a back door.
    """
    scopes = KeyScopes(["*"])

    assert scopes.unrestricted is False
    assert scopes.matches("sessions.read") is False
    assert scopes.matches("*") is True, "it is still an ordinary exact-match id"


def test_a_bare_star_alongside_real_scopes_grants_only_the_real_ones():
    """Mixing ``"*"`` into a list does not widen the list."""
    scopes = KeyScopes(["*", "wiki.read"])

    assert scopes.matches("wiki.read") is True
    assert scopes.matches("sessions.read") is False


# ── round-trip through the JSON key store: create → resolve → rotate ──────────
def make_store(tmp_path) -> KeyStore:
    """A JSON key store isolated to a temp dir — no shared data root."""
    return KeyStore(path=str(tmp_path / "api_keys.json"))


def test_a_scopeless_key_survives_create_resolve_and_rotate_as_explicitly_none(tmp_path):
    """``scopes=[]`` must still be ``[]`` after rotation — never coerced to ``None``.

    Rotation copies the source record field-for-field; a field DROPPED here
    would silently promote a deliberately-powerless key to unrestricted.
    """
    store = make_store(tmp_path)
    plaintext, created = store.create_scoped_key("svc", owner_subject="svc:1", scopes=[])

    assert created["scopes"] == []
    assert KeyScopes.from_record(created).matches("sessions.read") is False

    resolved = store.resolve_key(plaintext)
    assert resolved is not None
    assert resolved["scopes"] == [], "persistence must not drop an explicit empty list"
    assert KeyScopes.from_record(resolved).unrestricted is False

    rotated = store.rotate_key(created["id"])
    assert rotated is not None
    new_plaintext, new_record = rotated
    assert new_record["scopes"] == [], "rotation must not coerce [] to None"
    assert KeyScopes.from_record(new_record).unrestricted is False

    resolved_new = store.resolve_key(new_plaintext)
    assert resolved_new is not None
    assert resolved_new["scopes"] == []
    assert KeyScopes.from_record(resolved_new).matches("sessions.read") is False


def test_a_legacy_key_with_no_scopes_stays_unrestricted_through_rotation(tmp_path):
    """The other direction: an omitted field must not materialize as ``[]``."""
    store = make_store(tmp_path)
    plaintext, created = store.create_key("legacy")

    assert "scopes" not in created
    assert KeyScopes.from_record(created).unrestricted is True

    resolved = store.resolve_key(plaintext)
    assert resolved is not None
    assert KeyScopes.from_record(resolved).unrestricted is True

    rotated = store.rotate_key(created["id"])
    assert rotated is not None
    new_plaintext, new_record = rotated
    assert "scopes" not in new_record, "an absent field must stay absent, not become []"
    assert KeyScopes.from_record(new_record).unrestricted is True

    resolved_new = store.resolve_key(new_plaintext)
    assert resolved_new is not None
    assert KeyScopes.from_record(resolved_new).unrestricted is True


def test_a_non_empty_scope_set_round_trips_through_rotation(tmp_path):
    """The granted set is carried across a rotation unchanged."""
    store = make_store(tmp_path)
    plaintext, created = store.create_scoped_key(
        "svc", owner_subject="svc:1", scopes=["sessions.read", "wiki.*"]
    )

    resolved = store.resolve_key(plaintext)
    assert resolved is not None
    assert resolved["scopes"] == ["sessions.read", "wiki.*"]

    rotated = store.rotate_key(created["id"])
    assert rotated is not None
    new_plaintext, new_record = rotated
    assert new_record["scopes"] == ["sessions.read", "wiki.*"]

    carried = KeyScopes.from_record(store.resolve_key(new_plaintext))
    assert carried.matches("sessions.read") is True
    assert carried.matches("wiki.anything") is True
    assert carried.matches("keys.admin") is False


def test_create_scoped_key_with_scopes_omitted_mints_a_legacy_unrestricted_key(tmp_path):
    """``None`` means "omitted"; only ``[]`` starts the three-state law at mint time."""
    store = make_store(tmp_path)
    plaintext, created = store.create_scoped_key("svc", owner_subject="svc:1")

    assert "scopes" not in created
    assert KeyScopes.from_record(store.resolve_key(plaintext)).unrestricted is True


def test_rotation_revokes_the_original_so_the_old_secret_stops_resolving(tmp_path):
    """The scopes law would be moot if the pre-rotation secret still worked."""
    store = make_store(tmp_path)
    old_plaintext, created = store.create_scoped_key("svc", owner_subject="svc:1", scopes=[])

    rotated = store.rotate_key(created["id"])
    assert rotated is not None

    assert store.resolve_key(old_plaintext) is None
    assert store.resolve_key(rotated[0]) is not None


def test_the_scopes_state_is_readable_straight_off_a_stored_record(tmp_path):
    """``from_record`` is the ONE reader — no call site re-derives the three states.

    It normalizes the stored shape (a JSON ``list``) to a tuple; what must
    survive is the STATE, which is why an explicitly-scopeless record reads back
    as an empty tuple rather than ``None``.
    """
    store = make_store(tmp_path)
    _, unrestricted = store.create_key("legacy")
    _, scopeless = store.create_scoped_key("svc-a", scopes=[])
    _, narrowed = store.create_scoped_key("svc-b", scopes=["wiki.read"])

    assert KeyScopes.from_record(unrestricted).granted is None
    assert KeyScopes.from_record(scopeless).granted == ()
    assert KeyScopes.from_record(narrowed).granted == ("wiki.read",)
