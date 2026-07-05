// Search-domain visual vocabulary for the workspace SCG graph (#79) — the
// palette / labels / layer grouping injected into the shared 3D ``Graph3DView``
// engine (the SAME renderer the wiki Knowledge Graph uses; no fork, no
// per-domain canvas).
//
// Colours come from the existing ``--graph-*`` token family (reused
// semantically) plus one ghost token; never hand-pick a hex. Every map is
// exhaustive over the CLOSED unions in ``./types`` so a new kind is a ``tsc``
// error (the console convention).

import type { Graph3DTheme } from "../../wiki/Graph3DView";
import type { ScgEdgeKind, ScgGraphLayer, ScgNodeKind } from "./types";

// Theme-aware palette — reuse the existing ``--graph-*`` family semantically.
// capability≈function (amber/action), entity_type≈class (green/type),
// field≈method (cyan/leaf), route_recipe≈module (violet/composite),
// Memory shares the wiki memory token; unmapped is a dedicated ghost token.
const SCG_KIND_VAR: Record<ScgNodeKind, string> = {
  capability: "--graph-function",
  entity_type: "--graph-class",
  field: "--graph-method",
  route_recipe: "--graph-module",
  Memory: "--graph-memory",
  unmapped: "--graph-scg-unmapped",
};

const SCG_EDGE_VAR: Record<ScgEdgeKind, string> = {
  HAS_ENTITY: "--graph-class",
  HAS_FIELD: "--graph-method",
  SUPPORTS_QUERY: "--graph-edge-soft",
  PRODUCES: "--graph-function",
  CONSUMES: "--graph-module",
  RESOLVES_TO: "--graph-edge-relates",
  ANCHORS: "--graph-edge-anchor",
  RELATES: "--graph-edge-relates",
};

// Layer per kind — drives the per-layer toggle. ``unmapped`` ghosts ride the
// ``schema`` toggle (they stand in for un-mapped schema sources).
const SCG_KIND_LAYER: Record<ScgNodeKind, ScgGraphLayer> = {
  capability: "schema",
  entity_type: "schema",
  field: "schema",
  route_recipe: "schema",
  unmapped: "schema",
  Memory: "memory",
};

// ── Layer + kind metadata for the toolbar (closed-union maps) ──────────────

// Mirrors the closed ``ScgGraphLayer`` union. ``entity`` is reserved — no SCG
// kind maps to it yet (``SCG_KIND_LAYER`` has none), so its toggle never
// surfaces; kept so the order tracks the wire union as the SCG layer grows.
const SCG_LAYER_ORDER: ScgGraphLayer[] = ["schema", "memory", "entity"];

const SCG_LAYER_LABEL: Record<ScgGraphLayer, string> = {
  schema: "Capabilities",
  memory: "Memory",
  entity: "Entities",
};

const SCG_LAYER_DOT: Record<ScgGraphLayer, string> = {
  schema: "bg-[hsl(var(--graph-function))]",
  memory: "bg-[hsl(var(--graph-memory))]",
  entity: "bg-[hsl(var(--graph-entity))]",
};

const SCG_ALL_NODE_KINDS: ScgNodeKind[] = [
  "capability",
  "entity_type",
  "field",
  "route_recipe",
  "Memory",
  "unmapped",
];

/** Legend-dot class per kind — also read by the node inspector header. */
export const SCG_KIND_DOT: Record<ScgNodeKind, string> = {
  capability: "bg-[hsl(var(--graph-function))]",
  entity_type: "bg-[hsl(var(--graph-class))]",
  field: "bg-[hsl(var(--graph-method))]",
  route_recipe: "bg-[hsl(var(--graph-module))]",
  Memory: "bg-[hsl(var(--graph-memory))]",
  unmapped: "bg-[hsl(var(--graph-scg-unmapped))]",
};

/** Human label per kind — also read by the node inspector header. */
export const SCG_KIND_LABEL: Record<ScgNodeKind, string> = {
  capability: "Capability",
  entity_type: "Type",
  field: "Field",
  route_recipe: "Recipe",
  Memory: "Memory",
  unmapped: "Unmapped",
};

/** The SCG visual vocabulary, injected into the shared {@link Graph3DView}. */
export const SCG_GRAPH_THEME: Graph3DTheme = {
  allKinds: SCG_ALL_NODE_KINDS,
  kindVar: SCG_KIND_VAR,
  kindDot: SCG_KIND_DOT,
  kindLabel: SCG_KIND_LABEL,
  edgeVar: SCG_EDGE_VAR,
  kindLayer: SCG_KIND_LAYER,
  layerOrder: SCG_LAYER_ORDER,
  layerLabel: SCG_LAYER_LABEL,
  layerDot: SCG_LAYER_DOT,
  // No folder hierarchy in the SCG graph — folderVar / edgeContainVar / a rich
  // nodeLabel are all wiki-only, so they stay unset (node.label is the hover).
};
