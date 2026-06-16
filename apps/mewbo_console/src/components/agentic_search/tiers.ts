import type { SearchTier } from "../../types/agenticSearch"

// Tier = one budget knob (decomposition depth + probe fan-out) that ALSO
// presets the model driving the run (`scg.traversal.tier_models`) — see
// docs/features-search.md "Search tiers". Default is auto. The hints speak
// the budget's real currency (depth × probes), not vague speed adjectives.
//
// Shared by the SearchBar budget pill and the SearchScopeControl scope pill
// (#101) — one definition so the two surfaces can never drift. Lives in a
// non-component module so re-exporting it doesn't trip react-refresh.
export const TIERS: { id: SearchTier; name: string; hint: string }[] = [
  { id: "fast", name: "Fast", hint: "shallow · few probes" },
  { id: "auto", name: "Auto", hint: "balanced (default)" },
  { id: "deep", name: "Deep", hint: "max depth · wide fan-out" },
]
