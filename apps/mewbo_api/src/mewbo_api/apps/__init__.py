"""Mewbo Apps — LLM-powered, agent-built, trigger-maintained mini apps.

An "app" is a durable, versioned manifest (:class:`~mewbo_api.apps.models.AppSpec`)
bundling a multi-file stlite frontend, agent-authored data pipelines, the
triggers that keep them alive, and a per-app data namespace. This package is
the app-side home for the sub-product's contracts, persistence, routes, and
lifecycle — see ``docs/superpowers/specs/2026-07-17-mewbo-apps-design.md``
for the full design (backend engine + routes live here; the agent-side
plugin ships under ``apps/mewbo_api/src/mewbo_api/apps/plugin/``).

``models.py`` holds every Pydantic contract and is the single authority the
store, routes, and lifecycle modules import — see its module docstring for
the domain model.
"""
