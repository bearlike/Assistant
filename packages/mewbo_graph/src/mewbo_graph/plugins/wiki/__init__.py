"""Built-in ``wiki`` plugin — DeepWiki-style documentation backend.

Tools and agents are registered via the plugin manifest at
``.claude-plugin/plugin.json``. The wiki plugin is opt-in via the
``mewbo-api[wiki]`` extras; if those aren't installed, the API layer
guards in ``apps/mewbo_api/src/mewbo_api/wiki/__init__.py`` keep the
routes from mounting.
"""
