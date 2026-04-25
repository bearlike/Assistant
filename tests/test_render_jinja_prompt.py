from mewbo_core.common import render_jinja_prompt


def test_render_existing_ha_prompt():
    """The HA template should still render with vars (tests .txt+Jinja2 fallback)."""
    out = render_jinja_prompt(
        "homeassistant-set-state",
        ALL_ENTITIES="light.kitchen\nfan.bedroom",
    )
    assert "light.kitchen" in out


def test_render_missing_var_tolerates():
    """Missing vars should not crash — Jinja2 undefined is empty string."""
    out = render_jinja_prompt("homeassistant-set-state")
    assert isinstance(out, str)
