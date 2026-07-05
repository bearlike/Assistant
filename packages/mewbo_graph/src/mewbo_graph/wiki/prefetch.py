"""Pre-warm tree-sitter grammars for offline / air-gapped deployments.

``tree_sitter_language_pack.get_language()`` does NOT bundle every grammar in
the installed wheel (a prior assumption baked into this package's docstrings
and pins — see ``graph.py:_load_ts_language`` and this package's
``pyproject.toml``). Verified empirically against the resolved 1.12.x wheel:
the FIRST ``get_language(<lang>)`` call for a language fetches its parser
archive over the network and caches it under
``tree_sitter_language_pack.cache_dir()`` (``~/.cache/tree-sitter-language-pack/
v<ver>/libs/`` by default); nothing lands on disk at install time.

That first-use download is a landmine for offline/air-gapped deployments and
for the first real indexing run in a fresh container — a transient network
blip mid-parse would abort it. Run this module at IMAGE BUILD time (see
``docker/Dockerfile.api``) so every grammar ``GraphIndex`` needs is already on
disk before the container ever serves a request::

    python -m mewbo_graph.wiki.prefetch

The language list is derived from ``graph.py:_LANG_BY_EXT`` — the single
source of truth for "languages the code-graph extractor supports" — so this
module can never drift from what ``GraphIndex`` actually loads.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def main() -> int:
    """Warm the tree-sitter grammar cache for every language ``GraphIndex`` uses.

    Returns the process exit code: ``0`` on success, or on a clean skip when
    the ``treesitter`` extra isn't installed (an image without wiki extras
    needs no grammars — that is not a failure); ``1`` if the pack is
    installed but prefetching a grammar actually fails.
    """
    try:
        import tree_sitter_language_pack as tlp
    except ImportError:
        print(
            "mewbo_graph.wiki.prefetch: 'treesitter' extra not installed — "
            "skipping tree-sitter grammar pre-warm."
        )
        return 0

    from .graph import _LANG_BY_EXT

    languages = sorted(set(_LANG_BY_EXT.values()))
    logger.info("Pre-warming tree-sitter grammars: %s", ", ".join(languages))
    try:
        tlp.prefetch(languages)
    except Exception:
        logger.exception("Tree-sitter grammar pre-warm failed")
        return 1

    logger.info("Tree-sitter grammar pre-warm complete (%d languages)", len(languages))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
