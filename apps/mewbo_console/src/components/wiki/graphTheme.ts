/**
 * Shared graph theme primitives — the single home for the kind→colour,
 * kind→layer, edge→colour and degree→size scheme, plus the kind/layer
 * presentation maps (``ALL_NODE_KINDS`` / ``KIND_DOT`` / ``LAYER_*``). The
 * shared 3D ``Graph3DView`` reads these (via the wiki ``WIKI_GRAPH_THEME``
 * adapter) in its ``<ForceGraph3D>`` accessors; the Agentic-Search SCG graph
 * has a parallel ``SCG_GRAPH_THEME`` so both galaxies share one grammar.
 *
 * Kept as plain data + pure functions (no DOM/three imports beyond the live
 * CSS-var read) so it's importable from anywhere in the wiki subsystem. CSS-var
 * names mirror the ``--graph-*`` tokens defined in ``src/index.css``.
 */
import type { GraphEdgeKind, GraphLayer, GraphNodeKind } from "./api/types";

/** Theme-aware node fill, per kind (``--graph-*`` token names). */
export const KIND_VAR: Record<GraphNodeKind, string> = {
  File: "--graph-file",
  Module: "--graph-module",
  Class: "--graph-class",
  Function: "--graph-function",
  Method: "--graph-method",
  Interface: "--graph-interface",
  Object: "--graph-object",
  Property: "--graph-property",
  External: "--graph-external",
  Entity: "--graph-entity",
  Memory: "--graph-memory",
  // Folder supernodes reuse the module token but the galaxy overrides it via
  // FOLDER_VAR (kept distinct so a future folder-specific token is one edit).
  Folder: "--graph-module",
};

/** Theme-aware edge line colour, per kind. */
export const EDGE_VAR: Record<GraphEdgeKind, string> = {
  CONTAINS: "--graph-edge-soft",
  IMPORTS: "--graph-file",
  CALLS: "--graph-function",
  EXTENDS: "--graph-class",
  REFERENCES: "--graph-edge-soft",
  ANCHORS: "--graph-edge-anchor",
  RELATES: "--graph-edge-relates",
};

/** Multiplex layer a node kind belongs to. Exhaustive over ``GraphNodeKind``
 *  so ``tsc`` flags any new kind that forgets a layer. */
export const KIND_LAYER: Record<GraphNodeKind, GraphLayer> = {
  File: "ast",
  Module: "ast",
  Class: "ast",
  Function: "ast",
  Method: "ast",
  Interface: "ast",
  Object: "ast",
  Property: "ast",
  External: "ast",
  Entity: "entity",
  Memory: "memory",
  Folder: "ast",
};

/** All node kinds, in the toolbar's display order (Folder leads as the LOD
 *  scaffold). The single ordered list — the 3D view's kind chips read it. */
export const ALL_NODE_KINDS: GraphNodeKind[] = [
  "Folder",
  "File",
  "Module",
  "Class",
  "Function",
  "Method",
  "Interface",
  "Object",
  "Property",
  "External",
  "Entity",
  "Memory",
];

/** Tailwind ``bg-…`` legend-dot class per kind (toolbar chips / inspector). */
export const KIND_DOT: Record<GraphNodeKind, string> = {
  Folder: "bg-[hsl(var(--graph-module))]",
  File: "bg-[hsl(var(--graph-file))]",
  Module: "bg-[hsl(var(--graph-module))]",
  Class: "bg-[hsl(var(--graph-class))]",
  Function: "bg-[hsl(var(--graph-function))]",
  Method: "bg-[hsl(var(--graph-method))]",
  Interface: "bg-[hsl(var(--graph-interface))]",
  Object: "bg-[hsl(var(--graph-object))]",
  Property: "bg-[hsl(var(--graph-property))]",
  External: "bg-[hsl(var(--graph-external))]",
  Entity: "bg-[hsl(var(--graph-entity))]",
  Memory: "bg-[hsl(var(--graph-memory))]",
};

/** Multiplex layers in toolbar order + their labels / legend dots. */
export const LAYER_ORDER: GraphLayer[] = ["ast", "entity", "memory"];
export const LAYER_LABEL: Record<GraphLayer, string> = {
  ast: "Code",
  entity: "Entities",
  memory: "Memory",
};
export const LAYER_DOT: Record<GraphLayer, string> = {
  ast: "bg-[hsl(var(--graph-file))]",
  entity: "bg-[hsl(var(--graph-entity))]",
  memory: "bg-[hsl(var(--graph-memory))]",
};

/** The synthetic folder supernode token — folders ride the ``ast`` layer but
 *  carry their own fill so they read apart from the code discs. */
export const FOLDER_VAR = "--graph-module";

/** The faint hierarchy-backbone token — the re-pointed ``CONTAINS`` ties the
 *  galaxy draws subordinate to the relationship edges (folder→file→symbol,
 *  folder→subfolder) so the tree is visible and nothing floats. Rendered at a
 *  low alpha via ``cssVarColorAlpha`` so it recedes under the real edges. */
export const EDGE_CONTAIN_VAR = "--graph-edge-contain";

/**
 * Resolve a ``--graph-*`` CSS variable to a concrete colour the WebGL
 * renderer can consume. three.js chokes on the modern space-separated
 * ``hsl(H S% L%)`` form our tokens are stored in, so we normalise to the
 * comma form here. Returns a safe grey when there's no DOM (SSR / unit tests).
 */
export function cssVarColor(name: string): string {
  if (typeof window === "undefined") return "#999";
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  if (!raw) return "#999";
  const parts = raw.split(/\s+/);
  if (parts.length === 3) return `hsl(${parts[0]}, ${parts[1]}, ${parts[2]})`;
  return `hsl(${raw})`;
}

/**
 * Like ``cssVarColor`` but bakes an alpha channel into the colour (the comma
 * ``hsla(H, S%, L%, a)`` form ``three.js``/``tinycolor`` parse). The 3D
 * renderer has no per-link opacity accessor — its global ``linkOpacity`` is
 * MULTIPLIED by the colour's alpha — so a faint tie (e.g. the containment
 * backbone) bakes its extra fade here. ``alpha`` is clamped to ``[0,1]``.
 */
export function cssVarColorAlpha(name: string, alpha: number): string {
  const a = Math.min(1, Math.max(0, alpha));
  if (typeof window === "undefined") return `hsla(0, 0%, 60%, ${a})`;
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  if (!raw) return `hsla(0, 0%, 60%, ${a})`;
  const parts = raw.split(/\s+/);
  if (parts.length === 3) return `hsla(${parts[0]}, ${parts[1]}, ${parts[2]}, ${a})`;
  return `hsla(${raw}, ${a})`;
}

/**
 * Degree-weighted node size — the SAME log curve the 2D renderer uses
 * (``28 + 6·log2(1+d)`` clamped at 60), divided down to the smaller world
 * units a 3D sphere wants. Folder supernodes get a flat boost so they read
 * as containers, scaled by how many descendants they fold.
 */
export function nodeSize(degree: number, isFolder = false, folded = 0): number {
  const base = Math.min(60, 28 + 6 * Math.log2(1 + Math.max(0, degree)));
  if (!isFolder) return base / 6;
  // A collapsed folder stands in for everything inside it — size it by the
  // fold count (log-damped) so a 2000-file folder isn't 100× a 2-file one.
  return (base + 12 + 6 * Math.log2(1 + Math.max(0, folded))) / 5;
}
