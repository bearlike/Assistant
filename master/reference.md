# API Reference

This page is generated from inline docstrings via mkdocstrings. The sections below are grouped by package or client.

## packages/mewbo_core (core runtime)
::: mewbo_core.orchestrator

::: mewbo_core.task_master

::: mewbo_core.tool_use_loop

::: mewbo_core.agent_context

::: mewbo_core.hypervisor

::: mewbo_core.spawn_agent

::: mewbo_core.planning

::: mewbo_core.session_runtime

::: mewbo_core.session_store

::: mewbo_core.context

::: mewbo_core.compaction

::: mewbo_core.token_budget

::: mewbo_core.tool_registry

::: mewbo_core.classes

::: mewbo_core.types

::: mewbo_core.config

::: mewbo_core.components

::: mewbo_core.permissions

::: mewbo_core.hooks

::: mewbo_core.common

::: mewbo_core.errors

::: mewbo_core.notifications

::: mewbo_core.share_store

::: mewbo_core.llm

::: mewbo_core.plugins

::: mewbo_core.agent_registry

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
