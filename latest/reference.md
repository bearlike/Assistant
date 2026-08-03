# API Reference

This page is generated from inline docstrings via mkdocstrings. The sections below are grouped by package or client.

## packages/mewbo_core (core runtime)
::: mewbo_core.loop.orchestrator

::: mewbo_core.loop.task_master

::: mewbo_core.loop.tool_use_loop

::: mewbo_core.agents.agent_context

::: mewbo_core.agents.hypervisor

::: mewbo_core.agents.spawn_agent

::: mewbo_core.loop.planning

::: mewbo_core.loop.session_runtime

::: mewbo_core.session.session_store

::: mewbo_core.session.context

::: mewbo_core.session.compaction

::: mewbo_core.session.token_budget

::: mewbo_core.tooling.tool_registry

::: mewbo_core.classes

::: mewbo_core.contracts.types

::: mewbo_core.config

::: mewbo_core.components

::: mewbo_core.permissions

::: mewbo_core.hooks

::: mewbo_core.common

::: mewbo_core.contracts.errors

::: mewbo_core.session.notifications

::: mewbo_core.session.share_store

::: mewbo_core.llm.llm

::: mewbo_core.tooling.plugins

::: mewbo_core.agents.agent_registry

## packages/mewbo_tools (tool integrations)
::: mewbo_tools.integration.mcp

::: mewbo_tools.integration.homeassistant

::: mewbo_tools.integration.lsp

::: mewbo_tools.integration.lsp.manager

::: mewbo_tools.integration.lsp.servers

## packages/mewbo_graph (knowledge-graph capability library)
The optional substrate shared by MewboWiki and Mewbo Search. Requires the library extras (`treesitter`, `retrieval`); absent when uninstalled.

### MewboWiki substrate (`mewbo_graph.wiki`)
::: mewbo_graph.wiki.graph

::: mewbo_graph.wiki.structure_provider

::: mewbo_graph.wiki.embedder

::: mewbo_graph.wiki.retriever

::: mewbo_graph.wiki.memory

::: mewbo_graph.wiki.memory_types

::: mewbo_graph.wiki.store

::: mewbo_graph.wiki.types

### Source Capability Graph (`mewbo_graph.scg`)
::: mewbo_graph.scg.router

::: mewbo_graph.scg.parser

::: mewbo_graph.scg.entity_resolution

::: mewbo_graph.scg.memory_bridge

::: mewbo_graph.scg.store

::: mewbo_graph.scg.types

::: mewbo_graph.scg.providers

## Clients (apps/)
- API entry point: `apps/mewbo_api/src/mewbo_api/backend.py`
- Console: `apps/mewbo_console/` (React + Vite, connects via REST API)
- CLI entry point: `apps/mewbo_cli/src/mewbo_cli/cli_master.py`

## Home Assistant integration (mewbo_ha_conversation)
::: mewbo_ha_conversation.api

::: mewbo_ha_conversation.config_flow

::: mewbo_ha_conversation.const

::: mewbo_ha_conversation.coordinator

::: mewbo_ha_conversation.exceptions

::: mewbo_ha_conversation.helpers
