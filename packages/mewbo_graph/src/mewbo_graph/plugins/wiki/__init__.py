"""Built-in ``wiki`` plugin — auto-generated documentation backend.

Tools and agents are registered via the plugin manifest at
``.claude-plugin/plugin.json``. The wiki plugin is opt-in via the
``mewbo-api[wiki]`` extras; if those aren't installed, the API layer
guards in ``apps/mewbo_api/src/mewbo_api/wiki/__init__.py`` keep the
routes from mounting.

**Which entries are ``"unconditional": true`` — the selection rule.** The
bundle is gated on the ``wiki`` capability, but six read/navigate tools
(``wiki_list_pages``, ``wiki_read_page``, ``wiki_search_pages``,
``wiki_code_search``, ``wiki_query_graph``, ``wiki_graph_neighbors``) opt out
of that gate so an ordinary task session can consult an indexed wiki without
the client advertising anything. JSON carries no comments, so the rule lives
here:

    A tool is default-on IFF invoking it starts no new Mewbo session or run.

An outbound embedding call is acceptable; starting an agent run is not. That is
what keeps the pipeline tools (``wiki_clone_repo`` … ``wiki_finalize``), the
terminal ``wiki_emit_answer``, the writes (``wiki_submit_insight``,
``mint_entity``, ``relate_entities``), ``resolve_entity``, and the clone-access
readers (``wiki_read_file``, ``wiki_grep``, ``wiki_list_files``) behind the
capability gate. Note the gate the six shed is the CAPABILITY one only — a
STRICT ``allowed_tools`` scope still caps them, which is what preserves the
``wiki-qa`` root's ``QA_TOOLS`` ceiling (the ceiling that forces delegation to
probes rather than read-one-page-and-stop).
"""
