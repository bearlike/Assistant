// Render config for the workspace SCG graph (#79) — the search-domain palette /
// glyphs / shapes injected into the shared ``KnowledgeGraphRenderer`` engine.
//
// The renderer engine (cytoscape lifecycle, focus, filter, layout, theming) is
// domain-agnostic; this is the ONLY search-specific surface — it mirrors the
// wiki renderer's internal config maps (extracted to ``GraphRenderConfig``) for
// the SCG node/edge vocabulary. Colours come from the existing ``--graph-*``
// token family (reused semantically) plus one new ghost token; never hand-pick
// a hex. Every map is exhaustive over the CLOSED unions in ``./types`` so a new
// kind is a ``tsc`` error.

import {
  makeIconUri,
  type GraphRenderConfig,
  type IconElement,
} from "../../wiki/KnowledgeGraphRenderer";
import type { ScgEdgeKind, ScgGraphLayer, ScgNodeKind } from "./types";

// ── Lucide glyph paths (ISC, copied from lucide-react internals) ──────────
// One glyph per SCG node kind, matching the search domain semantics.
const SCG_ICON_PATHS: Record<ScgNodeKind, IconElement[]> = {
  // capability — lucide ``zap`` (an executable operation).
  capability: [["path", { d: "M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z" }]],
  // entity_type — lucide ``table-2`` (a schema type).
  entity_type: [
    ["path", { d: "M9 3H5a2 2 0 0 0-2 2v4m6-6h10a2 2 0 0 1 2 2v4M9 3v18m0 0h10a2 2 0 0 0 2-2V9M9 21H5a2 2 0 0 1-2-2V9m0 0h18" }],
  ],
  // field — lucide ``tag`` (a property on a type).
  field: [
    ["path", { d: "M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z" }],
    ["circle", { cx: "7.5", cy: "7.5", r: ".5", fill: "white" }],
  ],
  // route_recipe — lucide ``route`` (a precomputed pathway).
  route_recipe: [
    ["circle", { cx: "6", cy: "19", r: "3" }],
    ["path", { d: "M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15" }],
    ["circle", { cx: "18", cy: "5", r: "3" }],
  ],
  // Memory — lucide ``brain`` (the memory-orchestration layer; matches wiki).
  Memory: [
    ["path", { d: "M12 18V5" }],
    ["path", { d: "M15 13a4.17 4.17 0 0 1-3-4 4.17 4.17 0 0 1-3 4" }],
    ["path", { d: "M17.598 6.5A3 3 0 1 0 12 5a3 3 0 1 0-5.598 1.5" }],
    ["path", { d: "M17.997 5.125a4 4 0 0 1 2.526 5.77" }],
    ["path", { d: "M18 18a4 4 0 0 0 2-7.464" }],
    ["path", { d: "M19.967 17.483A4 4 0 1 1 12 18a4 4 0 1 1-7.967-.517" }],
    ["path", { d: "M6 18a4 4 0 0 1-2-7.464" }],
    ["path", { d: "M6.003 5.125a4 4 0 0 0-2.526 5.77" }],
  ],
  // unmapped — lucide ``circle-help`` (a source not yet mapped — ghost).
  unmapped: [
    ["circle", { cx: "12", cy: "12", r: "10" }],
    ["path", { d: "M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" }],
    ["path", { d: "M12 17h.01" }],
  ],
};

const SCG_ICON_URI: Record<ScgNodeKind, string> = {
  capability: makeIconUri(SCG_ICON_PATHS.capability),
  entity_type: makeIconUri(SCG_ICON_PATHS.entity_type),
  field: makeIconUri(SCG_ICON_PATHS.field),
  route_recipe: makeIconUri(SCG_ICON_PATHS.route_recipe),
  Memory: makeIconUri(SCG_ICON_PATHS.Memory),
  unmapped: makeIconUri(SCG_ICON_PATHS.unmapped),
};

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

// Layer per kind — drives the per-layer toggle. The renderer cascades a
// hidden layer's node kinds to its edges via the endpoint-hidden rule, so no
// edge kinds are enumerated here. ``unmapped`` ghosts ride the ``schema``
// toggle (they stand in for un-mapped schema sources).
const SCG_KIND_LAYER: Record<ScgNodeKind, ScgGraphLayer> = {
  capability: "schema",
  entity_type: "schema",
  field: "schema",
  route_recipe: "schema",
  unmapped: "schema",
  Memory: "memory",
};

// Non-disc silhouettes so the layers read apart at a glance (mirrors the wiki:
// memory=hexagon; the schema layer's composite/ghost kinds get distinct shapes).
const SCG_SHAPE: Partial<Record<ScgNodeKind, string>> = {
  entity_type: "round-rectangle",
  route_recipe: "round-diamond",
  Memory: "round-hexagon",
  unmapped: "round-diamond",
};

/** The full render config injected into ``KnowledgeGraphRenderer``. */
export const SCG_RENDER_CONFIG: GraphRenderConfig = {
  iconUri: SCG_ICON_URI,
  kindVar: SCG_KIND_VAR,
  edgeVar: SCG_EDGE_VAR,
  shape: SCG_SHAPE,
};

// ── Layer + kind metadata for the screen's toolbar (closed-union maps) ─────

export const SCG_LAYER_ORDER: ScgGraphLayer[] = ["schema", "memory", "entity"];

export const SCG_LAYER_LABEL: Record<ScgGraphLayer, string> = {
  schema: "Capabilities",
  memory: "Memory",
  entity: "Entities",
};

export const SCG_LAYER_DOT: Record<ScgGraphLayer, string> = {
  schema: "bg-[hsl(var(--graph-function))]",
  memory: "bg-[hsl(var(--graph-memory))]",
  entity: "bg-[hsl(var(--graph-entity))]",
};

export const SCG_ALL_NODE_KINDS: ScgNodeKind[] = [
  "capability",
  "entity_type",
  "field",
  "route_recipe",
  "Memory",
  "unmapped",
];

export const SCG_KIND_LABEL: Record<ScgNodeKind, string> = {
  capability: "Capability",
  entity_type: "Type",
  field: "Field",
  route_recipe: "Recipe",
  Memory: "Memory",
  unmapped: "Unmapped",
};

export const SCG_KIND_DOT: Record<ScgNodeKind, string> = {
  capability: "bg-[hsl(var(--graph-function))]",
  entity_type: "bg-[hsl(var(--graph-class))]",
  field: "bg-[hsl(var(--graph-method))]",
  route_recipe: "bg-[hsl(var(--graph-module))]",
  Memory: "bg-[hsl(var(--graph-memory))]",
  unmapped: "bg-[hsl(var(--graph-scg-unmapped))]",
};

/** The node kinds that compose a layer — drives the per-layer bulk toggle. */
export function scgKindsForLayer(layer: ScgGraphLayer): ScgNodeKind[] {
  return SCG_ALL_NODE_KINDS.filter((k) => SCG_KIND_LAYER[k] === layer);
}
