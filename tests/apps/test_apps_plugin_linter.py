"""Tests for the apps frontend linter (mewbo_api.apps.plugin.linter).

The apps linter IS the widget linter re-tabled: it reuses the parse-once-fan-out
runner and the finding shape, but with a different allowlist (adds ``mewbo_app``)
and a different rule set (page chrome allowed, dynamic-exec banned). These tests
pin the ALLOW/DENY table that delta produces.
"""

from __future__ import annotations

from mewbo_api.apps.plugin.linter import (
    ALLOWED_MODULES,
    check_pipeline_error_swallow,
    derive_collection_writes,
    lint_app,
)
from mewbo_core.builtin_plugins.widget_builder.linter import (
    ALLOWED_MODULES as WIDGET_ALLOWED_MODULES,
    lint,
)


def _rules(source: str) -> set[str]:
    return {f.rule for f in lint_app(source)}


def _pipeline_swallow_rules(source: str) -> set[str]:
    return {f.rule for f in lint(source, rules=(check_pipeline_error_swallow,))}


# ---------------------------------------------------------------------------
# Source-derived pipeline output contracts — a derivation, never a lint gate.
# ---------------------------------------------------------------------------


def test_derive_collection_writes_finds_direct_literal_mutations():
    source = (
        "def run(params, ctx):\n"
        "    ctx.collection('records').upsert('r1', {'value': 1})\n"
        "    ctx.collection(\"scratch\").delete('old')\n"
    )
    assert derive_collection_writes(source) == {"records", "scratch"}


def test_derive_collection_writes_ignores_dynamic_or_indirect_names():
    source = (
        "def run(params, ctx):\n"
        "    name = params['collection']\n"
        "    ctx.collection(name).upsert('r1', {})\n"
        "    collection = ctx.collection('indirect')\n"
        "    collection.upsert('r2', {})\n"
    )
    assert derive_collection_writes(source) == set()


# ---------------------------------------------------------------------------
# Allowlist — the widget set plus mewbo_app
# ---------------------------------------------------------------------------


def test_allowlist_is_widget_plus_sdk():
    assert "mewbo_app" in ALLOWED_MODULES
    assert WIDGET_ALLOWED_MODULES < ALLOWED_MODULES
    assert ALLOWED_MODULES - WIDGET_ALLOWED_MODULES == {"mewbo_app"}


def test_sdk_import_allowed():
    assert lint_app("import mewbo_app\nfrom mewbo_app import connect") == []


def test_streamlit_and_data_libs_allowed():
    assert lint_app("import streamlit as st\nimport pandas as pd\nimport altair") == []


# ---------------------------------------------------------------------------
# Page chrome is ALLOWED for apps (the delta from widgets)
# ---------------------------------------------------------------------------


def test_page_chrome_allowed():
    source = (
        "import streamlit as st\n"
        "st.header('h')\n"
        "st.subheader('s')\n"
        "st.divider()\n"
        "st.sidebar.title('nav')\n"
        "st.tabs(['a', 'b'])\n"
    )
    assert lint_app(source) == []


# ---------------------------------------------------------------------------
# Still banned: raw network, set_page_config, dynamic execution
# ---------------------------------------------------------------------------


def test_raw_http_clients_banned():
    for module in ("requests", "httpx", "urllib", "aiohttp", "socket", "js", "pyodide"):
        assert "unsupported-import" in _rules(f"import {module}"), module


def test_from_import_network_banned():
    assert "unsupported-import" in _rules("from urllib.request import urlopen")
    assert "unsupported-import" in _rules("from js import fetch")


def test_set_page_config_banned():
    assert "forbidden-set-page-config" in _rules(
        "import streamlit as st\nst.set_page_config(layout='wide')"
    )


def test_dynamic_execution_banned():
    assert "forbidden-dynamic-exec" in _rules("__import__('os')")
    assert "forbidden-dynamic-exec" in _rules("eval('1+1')")
    assert "forbidden-dynamic-exec" in _rules("exec('x = 1')")
    assert "forbidden-dynamic-exec" in _rules("compile('1', '<s>', 'eval')")
    assert "forbidden-dynamic-exec" in _rules("import importlib\nimportlib.import_module('os')")


def test_dynamic_import_defeats_allowlist_is_caught():
    # The whole reason apps ban dynamic exec: it would walk around the import allowlist.
    assert "forbidden-dynamic-exec" in _rules("mod = __import__('requests')")


# ---------------------------------------------------------------------------
# Reused runner behavior
# ---------------------------------------------------------------------------


def test_syntax_error_short_circuits():
    findings = lint_app("def broken(:\n    pass")
    assert [f.rule for f in findings] == ["syntax"]


def test_relative_import_ignored():
    assert lint_app("from . import helpers") == []


# NOTE: pipeline-file *linting* (`mode="code"` entrypoints) is NOT `lint_app`'s
# concern — `pipeline_runner.py:lint_pipeline`/`PIPELINE_ALLOWED_MODULES` is the
# ONE canonical pipeline lint (it also gates actual execution) — see
# `tests/apps/test_apps_pipeline_runner.py` for its coverage. `submit_app.py`
# imports `lint_pipeline` FROM there, not from this module. `lint_app` never runs
# `check_pipeline_error_swallow` below either (see `test_not_part_of_app_rules`)
# — that rule is defined here only so it shares the parse-once-fan-out runner,
# and is meant for `pipeline_runner.py`'s own pipeline rule table.


def test_clean_multi_page_app_passes():
    source = (
        "import streamlit as st\n"
        "import mewbo_app\n"
        "app = mewbo_app.connect()\n"
        "st.header('Dashboard')\n"
        "for row in app.data.query('emails'):\n"
        "    st.write(row)\n"
    )
    assert lint_app(source) == []


# ---------------------------------------------------------------------------
# Pipeline-only: a swallowed PipelineExecutionError disarms the submit verifier
# ---------------------------------------------------------------------------


def test_bare_except_around_read_file_flagged():
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        text = ctx.read_file('exports/actions/index.csv')\n"
        "    except:\n"
        "        text = ''\n"
        "    return text\n"
    )
    assert "swallowed-pipeline-error" in _pipeline_swallow_rules(source)


def test_except_exception_around_glob_flagged():
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        paths = ctx.glob('exports/*.csv')\n"
        "    except Exception:\n"
        "        paths = []\n"
        "    return paths\n"
    )
    assert "swallowed-pipeline-error" in _pipeline_swallow_rules(source)


def test_except_base_exception_flagged():
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        text = ctx.read_file('exports/actions/index.csv')\n"
        "    except BaseException:\n"
        "        text = ''\n"
        "    return text\n"
    )
    assert "swallowed-pipeline-error" in _pipeline_swallow_rules(source)


def test_tuple_except_including_exception_flagged():
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        text = ctx.read_file('exports/actions/index.csv')\n"
        "    except (ValueError, Exception):\n"
        "        text = ''\n"
        "    return text\n"
    )
    assert "swallowed-pipeline-error" in _pipeline_swallow_rules(source)


def test_explicit_pipeline_execution_error_catch_without_reraise_flagged():
    # PipelineExecutionError is never actually importable inside a pipeline's
    # sandboxed namespace (see the rule's docstring), but a literal `except
    # PipelineExecutionError:` still signals swallow-intent, so it is flagged
    # the same way as `Exception`/`BaseException`.
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        text = ctx.read_file('exports/actions/index.csv')\n"
        "    except PipelineExecutionError:\n"
        "        text = ''\n"
        "    return text\n"
    )
    assert "swallowed-pipeline-error" in _pipeline_swallow_rules(source)


def test_reraising_bare_handler_allowed():
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        text = ctx.read_file('exports/actions/index.csv')\n"
        "    except Exception:\n"
        "        raise\n"
        "    return text\n"
    )
    assert _pipeline_swallow_rules(source) == set()


def test_reraising_new_exception_allowed():
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        text = ctx.read_file('exports/actions/index.csv')\n"
        "    except Exception as exc:\n"
        "        raise RuntimeError('read failed') from exc\n"
        "    return text\n"
    )
    assert _pipeline_swallow_rules(source) == set()


def test_conditional_reraise_narrowed_on_code_allowed():
    # The motivating real-world shape: swallows on ONE tolerable branch (a
    # genuinely-missing optional file, narrowed on the structured `.code`
    # attribute) and re-raises everything else. `_handler_reraises` accepts
    # this — see its docstring for why "a raise somewhere in the handler" is
    # the deliberately-chosen bar over "every path re-raises".
    source = (
        "import csv\n"
        "import io\n"
        "\n"
        "\n"
        "def _read_csv(ctx, path):\n"
        "    try:\n"
        "        text = ctx.read_file(path)\n"
        "    except Exception as exc:\n"
        "        if getattr(exc, 'code', None) == 'read':\n"
        "            return []\n"
        "        raise\n"
        "    return list(csv.DictReader(io.StringIO(text)))\n"
    )
    assert _pipeline_swallow_rules(source) == set()


def test_conditional_reraise_narrowed_on_message_still_allowed():
    # Same shape but narrowed by sniffing str(exc) instead of exc.code — the
    # AST rule can't distinguish "narrows correctly" from "narrows fragily",
    # only "swallows vs. re-raises somewhere". Message-sniffing is fragile and
    # is called out in app-builder.md's authoring guidance instead of being
    # blocked here (blocking it would need semantic, not syntactic, analysis).
    source = (
        "def _read_csv(ctx, path):\n"
        "    try:\n"
        "        text = ctx.read_file(path)\n"
        "    except Exception as exc:\n"
        "        msg = str(exc).lower()\n"
        "        if 'no file at' in msg or 'not found' in msg:\n"
        "            return []\n"
        "        raise\n"
        "    return text\n"
    )
    assert _pipeline_swallow_rules(source) == set()


def test_narrow_unrelated_exception_allowed():
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        text = ctx.read_file('exports/actions/index.csv')\n"
        "    except ValueError:\n"
        "        text = ''\n"
        "    return text\n"
    )
    assert _pipeline_swallow_rules(source) == set()


def test_nested_deep_call_inside_try_flagged():
    # The guarded call sits three frames deep (for -> if -> with) inside the
    # try body — the rule must not stop at the try's immediate children.
    source = (
        "def run(params, ctx):\n"
        "    text = ''\n"
        "    try:\n"
        "        for name in ['a', 'b']:\n"
        "            if name:\n"
        "                with marker():\n"
        "                    text = ctx.read_file(f'exports/{name}.csv')\n"
        "    except Exception:\n"
        "        text = ''\n"
        "    return text\n"
    )
    assert "swallowed-pipeline-error" in _pipeline_swallow_rules(source)


def test_clean_pipeline_no_try_allowed():
    source = (
        "def run(params, ctx):\n"
        "    text = ctx.read_file('exports/actions/index.csv')\n"
        "    return text\n"
    )
    assert _pipeline_swallow_rules(source) == set()


def test_clean_pipeline_try_without_guarded_call_allowed():
    source = (
        "def run(params, ctx):\n"
        "    try:\n"
        "        value = int(params.get('n', '0'))\n"
        "    except Exception:\n"
        "        value = 0\n"
        "    text = ctx.read_file('exports/actions/index.csv')\n"
        "    return {'value': value, 'text': text}\n"
    )
    assert _pipeline_swallow_rules(source) == set()


def test_not_part_of_app_rules():
    # `lint_app` (the frontend/widget rule table) never runs this pipeline-only
    # rule — a browser frontend file has no reason to call ctx.read_file/glob,
    # and the module docstring is explicit that pipeline checks live for
    # `pipeline_runner.py`'s table, not `APP_RULES`.
    source = (
        "def handler(ctx):\n"
        "    try:\n"
        "        text = ctx.read_file('x')\n"
        "    except Exception:\n"
        "        text = ''\n"
        "    return text\n"
    )
    assert "swallowed-pipeline-error" not in _rules(source)


def test_rule_is_wired_into_the_pipeline_rule_table():
    """The rule must gate a REAL submit, not merely exist.

    `lint_pipeline` is the entry point both boundaries share — `submit_app`
    routes `mode="code"` entrypoints to it, and `AppPipelineRunner` re-runs it
    at execution. A rule that lints correctly in isolation but is absent from
    `_PIPELINE_RULES` refuses nothing, so assert through the wired seam rather
    than the rule function.
    """
    from mewbo_api.apps.pipeline_runner import lint_pipeline

    swallowing = (
        "def _read(ctx, path):\n"
        "    try:\n"
        "        return ctx.read_file(path)\n"
        "    except Exception:\n"
        "        return ''\n"
        "def run(params, ctx):\n"
        "    return {'text': _read(ctx, 'exports/index.csv')}\n"
    )
    findings = lint_pipeline(swallowing)
    assert any(f.rule == "swallowed-pipeline-error" for f in findings)

    # A conditional re-raise engages with propagation: the failure the verifier
    # classifies on still escapes, so it must NOT be refused.
    conditional_reraise = (
        "def _read(ctx, path):\n"
        "    try:\n"
        "        return ctx.read_file(path)\n"
        "    except Exception as exc:\n"
        "        if 'no file at' in str(exc):\n"
        "            return ''\n"
        "        raise\n"
        "def run(params, ctx):\n"
        "    return {'text': _read(ctx, 'exports/index.csv')}\n"
    )
    assert not [
        f for f in lint_pipeline(conditional_reraise)
        if f.rule == "swallowed-pipeline-error"
    ]
