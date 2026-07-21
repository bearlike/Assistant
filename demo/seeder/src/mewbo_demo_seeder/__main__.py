#!/usr/bin/env python3
"""CLI boundary for the demo seeder — the one and only I/O edge.

    python -m mewbo_demo_seeder --bundle PATH [--t0 ISO]

Constructs the env-driven Mongo stores (``MEWBO_MONGODB_URI`` /
``MEWBO_MONGODB_DATABASE``), loads + validates the bundle JSON via the Pydantic
model, runs :class:`~mewbo_demo_seeder.seeder.DemoSeeder`, and exits non-zero
with a clear message on any failure. It runs on the api image's Python with only
that image's deps (``mewbo_core`` + ``pydantic`` + ``pymongo``) — no install
step in-container, ``PYTHONPATH=/demo/seeder/src`` is enough.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from mewbo_core.key_store_mongo import MongoKeyStore
from mewbo_core.session_store_mongo import MongoSessionStore
from mewbo_core.triggers.store_mongo import MongoTriggerStore

from mewbo_demo_seeder.models import SeedBundle
from mewbo_demo_seeder.seeder import DemoSeeder
from mewbo_demo_seeder.settle import NotificationSettler

_DEFAULT_T0 = "2026-07-14T09:30:00Z"


def _parse_t0(raw: str) -> datetime:
    """Parse an ISO-8601 ``--t0`` into an aware UTC datetime.

    Accepts a trailing ``Z`` (explicitly, for parity across 3.10) and stamps
    UTC on a naive instant rather than guessing a local zone.
    """
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    """Run the seeder CLI; return a process exit code."""
    parser = argparse.ArgumentParser(
        prog="mewbo_demo_seeder",
        description="Seed a demo Mewbo database deterministically from a bundle.",
    )
    parser.add_argument("--bundle", required=True, help="Path to the seed bundle JSON.")
    parser.add_argument(
        "--t0",
        default=os.environ.get("DEMO_T0", _DEFAULT_T0),
        help=f"Frozen T0 instant (ISO-8601). Default: $DEMO_T0 or {_DEFAULT_T0}.",
    )
    parser.add_argument(
        "--wiki-bundle",
        default=None,
        help="Optional wiki seed bundle JSON (projects/pages/graph/jobs/Q&A).",
    )
    parser.add_argument(
        "--search-bundle",
        default=None,
        help="Optional agentic-search seed bundle JSON (workspaces/runs/SCG).",
    )
    args = parser.parse_args(argv)

    try:
        t0 = _parse_t0(args.t0)
    except ValueError as exc:
        print(f"error: invalid --t0 {args.t0!r}: {exc}", file=sys.stderr)
        return 2

    try:
        with open(args.bundle, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read bundle {args.bundle!r}: {exc}", file=sys.stderr)
        return 2

    try:
        bundle = SeedBundle.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError et al.
        print(f"error: invalid bundle {args.bundle!r}: {exc}", file=sys.stderr)
        return 2

    # Local attachment root — the demo uses none, so a throwaway temp dir keeps
    # the seeder from writing under an arbitrary container cwd.
    session_dir = os.environ.get("DEMO_SESSION_DIR") or os.path.join(
        tempfile.gettempdir(), "mewbo-demo-sessions"
    )
    try:
        session_store = MongoSessionStore(root_dir=session_dir)
        trigger_store = MongoTriggerStore()
        # The key store reads storage.mongodb.* CONFIG (like the wiki store),
        # not the MEWBO_MONGODB_* env the session store honours — pass the env
        # explicitly so both halves of the seed land in the SAME demo db.
        key_store = MongoKeyStore(
            uri=os.environ["MEWBO_MONGODB_URI"],
            database=os.environ["MEWBO_MONGODB_DATABASE"],
        )
    except KeyError as exc:
        print(f"error: seeding requires env var {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # ConnectionError when Mongo is unreachable
        print(f"error: cannot connect to MongoDB: {exc}", file=sys.stderr)
        return 1

    try:
        report = DemoSeeder(
            session_store=session_store,
            trigger_store=trigger_store,
            bundle=bundle,
            t0=t0,
            key_store=key_store,
        ).seed()
    except Exception as exc:
        print(f"error: seeding failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"seeded {len(report.sessions)} session(s) and "
        f"{len(report.triggers)} trigger(s) at T0={t0.isoformat()}"
    )
    for sid in report.sessions:
        print(f"  session {sid}")
    for tid in report.triggers:
        print(f"  trigger {tid}")
    for kid in report.api_keys:
        print(f"  api key {kid}")

    # Optional wiki world (projects/pages/graph/jobs/Q&A), seeded THROUGH the
    # wiki store contracts. MongoWikiStore reads storage.mongodb.* config, NOT
    # the MEWBO_MONGODB_* env the session store honours, so build it explicitly.
    if args.wiki_bundle:
        from mewbo_graph.wiki.store import MongoWikiStore

        from mewbo_demo_seeder.wiki import WikiSeedBundle, WikiSeeder

        try:
            with open(args.wiki_bundle, encoding="utf-8") as handle:
                wiki_bundle = WikiSeedBundle.model_validate(json.load(handle))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read wiki bundle {args.wiki_bundle!r}: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # pydantic ValidationError et al.
            print(f"error: invalid wiki bundle {args.wiki_bundle!r}: {exc}", file=sys.stderr)
            return 2
        try:
            wiki_store = MongoWikiStore(
                uri=os.environ["MEWBO_MONGODB_URI"],
                database=os.environ["MEWBO_MONGODB_DATABASE"],
            )
            wiki_report = WikiSeeder(
                wiki_store=wiki_store, bundle=wiki_bundle, t0=t0
            ).seed()
        except KeyError as exc:
            print(f"error: wiki seeding requires env var {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"error: wiki seeding failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"seeded wiki: {len(wiki_report.projects)} project(s), "
            f"{wiki_report.pages} page(s), {wiki_report.graph_nodes} graph node(s), "
            f"{wiki_report.graph_edges} edge(s), {len(wiki_report.jobs)} job(s), "
            f"{len(wiki_report.qa)} Q&A"
        )

    # Optional agentic-search world (workspaces/runs + the SCG capability graph).
    # The workspace/run stores are env-driven; the SCG + memory-note layers ride
    # the wiki extra, so they are constructed behind an import guard — absent it,
    # the schema-only capability graph still renders and memory notes are skipped.
    if args.search_bundle:
        from mewbo_api.agentic_search.store import MongoAgenticSearchStore

        from mewbo_demo_seeder.search import SearchBundle, SearchSeeder

        try:
            with open(args.search_bundle, encoding="utf-8") as handle:
                search_bundle = SearchBundle.model_validate(json.load(handle))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"error: cannot read search bundle {args.search_bundle!r}: {exc}",
                file=sys.stderr,
            )
            return 2
        except Exception as exc:  # pydantic ValidationError et al.
            print(f"error: invalid search bundle {args.search_bundle!r}: {exc}", file=sys.stderr)
            return 2
        scg_store = None
        wiki_store_for_scg = None
        try:
            from mewbo_graph.scg.store import create_scg_store
            from mewbo_graph.wiki.store import create_wiki_store

            scg_store = create_scg_store()
            wiki_store_for_scg = create_wiki_store()
        except ImportError:
            pass  # wiki extra absent: schema graph renders, memory notes skipped
        try:
            search_report = SearchSeeder(
                search_store=MongoAgenticSearchStore(),
                bundle=search_bundle,
                t0=t0,
                scg_store=scg_store,
                wiki_store=wiki_store_for_scg,
            ).seed()
        except Exception as exc:
            print(f"error: search seeding failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"seeded search: {len(search_report.workspaces)} workspace(s), "
            f"{len(search_report.runs)} run(s), {search_report.scg_nodes} SCG node(s), "
            f"{search_report.scg_edges} edge(s), {search_report.memory_notes} memory note(s)"
        )

    # Settle completion notifications so no capture ever races a toast —
    # see settle.py for why this must happen over REST and why once is enough.
    # Opt-in via env: absent when tests drive the CLI against mongomock only.
    api_base = os.environ.get("DEMO_API_BASE")
    api_key = os.environ.get("DEMO_API_KEY")
    if api_base and api_key:
        try:
            dismissed = NotificationSettler(
                base_url=api_base,
                api_key=api_key,
                session_ids=report.sessions,
            ).settle()
        except Exception as exc:
            print(f"error: notification settling failed: {exc}", file=sys.stderr)
            return 1
        print(f"settled notifications ({dismissed} dismissed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
