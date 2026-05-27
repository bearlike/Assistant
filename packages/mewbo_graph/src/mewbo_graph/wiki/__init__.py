"""MewboWiki substrate — code graph, multiplex memory, embedder, retriever, store.

The reusable engine behind the wiki product. The API layer
(``mewbo_api.wiki``) is a thin HTTP/SSE shell over these classes; the wiki
SessionTools (``mewbo_graph.plugins.wiki``) drive them. Imports strictly down
into ``mewbo_core``; heavy deps (tree-sitter) are import-guarded so the
package is usable without the ``treesitter`` extra.
"""
from __future__ import annotations
