#!/usr/bin/env python3
"""Export the Flask-RESTX OpenAPI (Swagger 2.0) spec for the docs site.

Imports the API app headlessly, captures ``api.__schema__``, enriches it for
human consumption, and writes it to ``docs/openapi.json``. The docs page
``docs/rest-api.md`` renders this file as a full-page Scalar API reference
under the brand header (theme template ``app.html`` + ``header_tabs``).

Enrichment:
- intro markdown (``info.description``) with auth, quickstart, and conventions
- operations re-tagged by path into human resource families (the live schema
  puts every ``/api/*`` route under one giant ``api`` tag)
- ``x-tagGroups`` sidebar groups over the friendly tags
- light sanitation of summaries/descriptions (RST literals, trailing periods)
  as defense against future docstring drift

The output file is committed. CI regenerates it best-effort before the docs
build; when the API app is not importable there, the committed copy serves.
Exits with code 0 (unchanged) or 1 (updated / error), matching
``generate_config_schema.py``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_OUTPUT_PATH = REPO_ROOT / "docs" / "openapi.json"

# Markdown rendered by Scalar as the reference's introduction section.
OVERVIEW_DESCRIPTION = """\
Everything the Mewbo web console can do is available over plain HTTP. Create \
sessions and send queries. Stream events live. Run multi-source searches. Get \
schema-constrained answers for pipelines. Every endpoint below is generated \
from the running server, so this reference is always current with the code.

## Base URL

A self-hosted stack serves the API on port `5125` by default:

```
http://localhost:5125
```

Replace the host with wherever your stack runs. The request samples on this \
page use this base URL.

## Authentication

Every endpoint expects an API key in the `X-API-KEY` header:

```bash
curl -H "X-API-KEY: $MEWBO_API_KEY" http://localhost:5125/api/models
```

Two kinds of keys work. The master token from `configs/app.json` \
(`api.master_token`) always works, and it is the only key allowed to mint \
others via `POST /api/keys`. Minted keys work everywhere else and can be \
revoked individually, so give each integration its own.

Browsers cannot set headers on `EventSource` connections. Server-sent event \
endpoints therefore also accept the key as a query parameter: \
`?api_key=<key>`.

## Make your first request

Three calls take you from nothing to a live agent run:

```bash
# 1. Create a session
curl -X POST http://localhost:5125/api/sessions \\
  -H "X-API-KEY: $MEWBO_API_KEY" -H "Content-Type: application/json" -d '{}'
# -> {"session_id": "9e2d47c1..."}

# 2. Send it a query
curl -X POST http://localhost:5125/api/sessions/9e2d47c1.../query \\
  -H "X-API-KEY: $MEWBO_API_KEY" -H "Content-Type: application/json" \\
  -d '{"query": "Summarize the open pull requests."}'

# 3. Watch the run live
curl -N "http://localhost:5125/api/sessions/9e2d47c1.../stream?api_key=$MEWBO_API_KEY"
```

The stream closes when the run finishes. The full transcript stays available \
through `GET /api/sessions/{session_id}/events`.

## Long-running work

Searches, structured runs, and indexing jobs can outlive a single request. \
These endpoints return a run or job id right away. Poll the matching `GET` \
endpoint, or subscribe to the run's `/events` stream, until the status turns \
terminal. Structured run handles have the form `<session_id>:r<seq>`, so the \
part before the first colon is always a session you can stream.

## Streaming

Endpoints ending in `/stream` or `/events` speak [server-sent events]\
(https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events). \
Connect once and keep reading. Events are pushed the moment they happen; \
there is no polling interval to tune.

## Errors

Failures return a JSON body, never an HTML page. Expect \
`{"message": "..."}` on auth and validation errors, and \
`{"error": {"code", "reason", "retryable"}}` from the structured and search \
surfaces. Unknown routes return the same JSON envelope with a 404.

## Related guides

The [Web Console + API](../clients-web-api/) guide covers concepts like \
capability negotiation and sharing. [Building a Client](../developer-guide/) \
walks through a minimal integration. The Agentic Wiki has its own HTTP \
surface under `/v1/wiki/*`, documented in the \
[Agentic Wiki](../features-wiki/) guide.
"""

# The live schema tags every ``/api/*`` route with one giant ``api`` tag,
# which renders as a single unreadable sidebar bucket. Re-tag operations by
# path into resource families instead. First matching prefix wins; order
# matters (most specific first within a family).
RETAG_RULES: list[tuple[str, str]] = [
    ("/api/sessions", "Sessions"),
    ("/api/query", "Sessions"),
    ("/api/commands", "Sessions"),
    ("/api/share", "Sessions"),
    ("/api/projects", "Projects & Worktrees"),
    ("/api/v_projects", "Projects & Worktrees"),
    ("/api/keys", "API Keys"),
    ("/api/models", "Models"),
    ("/api/tools", "Tools & Skills"),
    ("/api/skills", "Tools & Skills"),
    ("/api/plugins", "Plugins"),
    ("/api/notifications", "Notifications"),
    ("/api/config", "Configuration"),
    ("/api/agentic_search/workspaces", "Workspaces & Runs"),
    ("/api/agentic_search/runs", "Workspaces & Runs"),
    ("/api/agentic_search/sources", "Source Graph"),
    ("/api/agentic_search/scg", "Source Graph"),
    ("/v1/structured/fast", "Fast Structured"),
    ("/v1/structured", "Structured Outputs"),
    ("/v1/draft", "Draft Streaming"),
    ("/v1/wiki", "Wiki Ingestion"),
    ("/api/automation", "Automation"),
]

# Sidebar groups (rendered by Scalar via the x-tagGroups vendor extension).
# Tags missing from the live schema are dropped; tags the schema grows that
# are not listed here land in a trailing "Other" group so nothing vanishes.
TAG_GROUPS: list[tuple[str, list[str]]] = [
    ("Sessions", ["Sessions"]),
    ("Projects", ["Projects & Worktrees"]),
    ("Agentic Search", ["Workspaces & Runs", "Source Graph"]),
    (
        "Structured & Realtime",
        ["Structured Outputs", "Fast Structured", "Draft Streaming", "Wiki Ingestion"],
    ),
    (
        "Platform",
        ["Models", "Tools & Skills", "Plugins", "Notifications", "Configuration", "API Keys"],
    ),
    ("Automation", ["Automation"]),
]

TAG_DESCRIPTIONS: dict[str, str] = {
    "Sessions": (
        "A session is the unit of conversation and audit. Create one, send it "
        "queries, steer it while it runs, and read everything back: events, "
        "sub-agent activity, token usage, diffs, and exports. Streams are "
        "push-based server-sent events. This is the surface the web console "
        "runs on."
    ),
    "Projects & Worktrees": (
        "Projects point sessions at a working directory. Managed projects can "
        "be created over the API; git-backed projects additionally expose "
        "branches and isolated worktrees, so several sessions can work the "
        "same repository without stepping on each other."
    ),
    "API Keys": (
        "Mint, list, and revoke API keys. Only the master token may mint. "
        "Give each integration its own revocable key."
    ),
    "Models": "Discover which LLM models the configured proxy currently serves.",
    "Tools & Skills": (
        "Introspect the tool registry and available skills, optionally scoped "
        "to a project. Useful for building pickers and capability checks."
    ),
    "Plugins": (
        "List installed plugins, browse configured marketplaces, and install "
        "or uninstall plugins. Plugin components load into new sessions."
    ),
    "Notifications": "Read and dismiss the notification feed shown in the console.",
    "Configuration": (
        "Read and patch the server configuration. Secrets are write-only and "
        "protected fields are never exposed; the schema endpoint describes "
        "exactly what a client may render and edit."
    ),
    "Workspaces & Runs": (
        "Agentic Search over saved multi-source workspaces. Create a run "
        "against a workspace, then poll it or follow its event stream to a "
        "cited answer."
    ),
    "Source Graph": (
        "Source catalog and Source Capability Graph (SCG) indexing. Map jobs "
        "index a source's reachable schemas and pathways so search can route "
        "through them. Requires the SCG feature to be enabled."
    ),
    "Structured Outputs": (
        "Schema-constrained agentic runs. Send a query plus a JSON Schema and "
        "get back an object that validates against it, with optional "
        "workspace grounding and graph-first provenance."
    ),
    "Fast Structured": (
        "Retrieval-only structured synthesis in one round trip. No tool use. "
        "Same request contract as /v1/structured, tuned for fast first tokens."
    ),
    "Draft Streaming": (
        "Token-by-token draft answers over server-sent events. The lowest "
        "latency surface; no schema, no tool use."
    ),
    "Wiki Ingestion": (
        "Direct document ingestion into a wiki project's catalog, for "
        "non-git sources."
    ),
    "Automation": (
        "CI and VCS automation. Lets a pipeline hand issues and pull "
        "requests to Mewbo agents (see the CI Agent Pickup guide)."
    ),
}

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# ``literal`` (RST) -> `literal` (markdown). Defense against docstring drift.
_RST_LITERAL = re.compile(r"``([^`]+)``")


class OpenApiSpecExporter:
    """Captures the live Flask-RESTX schema and decorates it for the docs."""

    @staticmethod
    def capture() -> dict[str, Any]:
        """Import the API app headlessly and return its live Swagger schema."""
        from mewbo_api.backend import api, app  # noqa: PLC0415 — heavy import

        with app.test_request_context():
            return json.loads(json.dumps(api.__schema__))

    @staticmethod
    def _tag_for_path(path: str) -> str:
        for prefix, tag in RETAG_RULES:
            if path.startswith(prefix):
                return tag
        # Defensive fallback for new route families: first meaningful segment.
        segments = [s for s in path.split("/") if s and s not in ("api", "v1")]
        return segments[0].replace("_", " ").title() if segments else "Other"

    @staticmethod
    def _clean_summary(summary: str) -> str:
        summary = _RST_LITERAL.sub(r"`\1`", summary).strip()
        return summary.rstrip(".")

    @classmethod
    def retag_and_sanitize(cls, spec: dict[str, Any]) -> set[str]:
        """Re-tag every operation by path; return the set of tags in use."""
        used: set[str] = set()
        for path, ops in spec.get("paths", {}).items():
            tag = cls._tag_for_path(path)
            for method, op in ops.items():
                if method not in HTTP_METHODS or not isinstance(op, dict):
                    continue
                op["tags"] = [tag]
                used.add(tag)
                if op.get("summary"):
                    op["summary"] = cls._clean_summary(op["summary"])
                if op.get("description"):
                    op["description"] = _RST_LITERAL.sub(r"`\1`", op["description"])
        return used

    @classmethod
    def enrich(cls, spec: dict[str, Any]) -> dict[str, Any]:
        """Add intro prose, resource tags, and sidebar groups to the spec."""
        spec.setdefault("info", {})["description"] = OVERVIEW_DESCRIPTION
        spec["host"] = "localhost:5125"
        spec["schemes"] = ["http"]

        used = cls.retag_and_sanitize(spec)

        groups = [
            {"name": name, "tags": [t for t in tags if t in used]}
            for name, tags in TAG_GROUPS
        ]
        groups = [g for g in groups if g["tags"]]
        grouped = {t for g in groups for t in g["tags"]}
        leftover = sorted(used - grouped)
        if leftover:
            groups.append({"name": "Other", "tags": leftover})
        spec["x-tagGroups"] = groups

        # Rebuild the tag list in sidebar order with docs-facing descriptions.
        spec["tags"] = [
            {"name": t, "description": TAG_DESCRIPTIONS.get(t, "")}
            for g in groups
            for t in g["tags"]
        ]
        return spec

    @classmethod
    def render(cls) -> str:
        """Capture, enrich, and serialize the spec to indented JSON text.

        ``sort_keys`` canonicalizes key order. Flask-RESTX emits response and
        definition maps in a non-deterministic order across processes, which
        would otherwise churn the committed file on every regeneration. Scalar
        renders from semantic fields and ``x-tagGroups``, so key order has no
        effect on the rendered reference.
        """
        spec = cls.enrich(cls.capture())
        return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def main() -> int:
    """Export the enriched OpenAPI spec to docs/openapi.json."""
    new_spec = OpenApiSpecExporter.render()

    if SPEC_OUTPUT_PATH.exists():
        if SPEC_OUTPUT_PATH.read_text(encoding="utf-8") == new_spec:
            print(f"OpenAPI spec unchanged: {SPEC_OUTPUT_PATH}")
            return 0

    SPEC_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_OUTPUT_PATH.write_text(new_spec, encoding="utf-8")
    print(f"OpenAPI spec updated: {SPEC_OUTPUT_PATH}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
