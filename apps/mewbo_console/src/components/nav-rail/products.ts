import { AppWindow, BookOpen, ListChecks, Search, type LucideIcon } from "lucide-react";

/** The four products the switcher offers, in `PRODUCTS` order. */
export type Product = "tasks" | "wiki" | "search" | "apps";

/**
 * What the rail is currently SCOPED to — which is a wider question than which
 * product row is current, and the two answers differ on exactly one route.
 *
 * `"settings"` is deliberately not a `Product`: it has no switcher row, so no
 * row is marked `aria-current` there. It scopes zone 3 all the same, because
 * that zone means "what is inside the current scope" rather than "recents"
 * specifically — for a product that is its recents, for Settings it is the
 * facet list, and those facet rows are what keep `/settings` from being a route
 * with no primary navigation. `null` (any other unscoped route) falls back to
 * Tasks, the home product.
 */
export type ActiveProduct = Product | "settings" | null;

/** Product-switcher metadata — the four rows, in order, with their landing
 *  route and icon. Icons reuse today's NavBar strip glyphs. */
export const PRODUCTS: ReadonlyArray<{
  id: Product;
  label: string;
  path: string;
  icon: LucideIcon;
}> = [
  { id: "tasks", label: "Tasks", path: "/", icon: ListChecks },
  { id: "wiki", label: "Wiki", path: "/wiki", icon: BookOpen },
  { id: "search", label: "Search", path: "/search", icon: Search },
  { id: "apps", label: "Apps", path: "/apps", icon: AppWindow },
];
