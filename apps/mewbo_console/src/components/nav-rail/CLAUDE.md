> ↑ [console /apps/mewbo_console/CLAUDE.md](../../../CLAUDE.md) · [root /CLAUDE.md](../../../../../CLAUDE.md)

# NavRail — the console's single navigation surface

## `rows.tsx` IS the rail vocabulary — hand-rolled rail chrome is a review reject

Section chrome, rows and the metrics both are built from live in ONE module, imported by every
rail-like surface: the NavRail's own sections, the Apps instrument rail, the Agentic Search right
rail, the wiki's on-page ToC. **If the kit cannot express what a surface needs, the kit gains a prop —
it does not get forked.** This is not tidiness. Five surfaces each spelling their own row is how one
list ended up at two heights, how one focus ring stayed diluted in three places, and why the same
missing border kept being reported on a new screen every time; a per-screen fix cannot converge
because the next screen was never touched.

| Export | What it is |
|---|---|
| `RailSection` | Section container: optional heading, the boundary above it, the gap below. A rail zone is a STACK of these |
| `RailSectionHeader` | The eyebrow alone (`icon`, `trailing`, `divider`), for when you don't want the container |
| `RailRow` | THE navigation row — recents, page trees, ToC entries. `leading`/`trailing`/`active`/`depth` |
| `RailActionRow` | The `h-8` icon+label action/product row |
| `RailDateGroup`, `RailEmpty` | Date bucket; calm empty state |
| `FOCUS_RING`, `railRowCls`, `railActionRowCls`, `railRowIconCls`, `railLabelIconCls`, `RAIL_PAD_X` | The metrics, for the one consumer that genuinely cannot render `RailRow` |

**The right-hand rails share the vocabulary, not the silhouette.** `agentic_search/RightRail`
renders the kit's section chrome directly (`RailSection`, `RailRow`, `FOCUS_RING`, `RAIL_PAD_X`).
The Apps instrument rail shares the type ramp, the glyph sizes and `RailEmpty`, but its section
chrome is a persisted Collapsible cluster with a collapsed icon-strip mode — axes `RailSection`
deliberately does not model — and its ROW (`apps/railSections/rows.tsx`) is its own component.
That boundary is considered, not drift: an instrument row carries an `actions` slot, so making the
whole row a `<button>` would nest interactive elements (invalid HTML, broken for keyboard and AT);
its anatomy is two-line where `RailRow` is fixed-height single-line; and the console's shape law
puts left surfaces at `rounded-lg` and right-hand instrument surfaces at `rounded-md`. Merging them
would need `as`, `actions`, multi-line, tone and radius axes on one component — the variant
explosion the kit exists to avoid. Share the tokens, not the silhouette.

The rail is the ONLY site navigation. There is no top navigation bar anywhere, on any route, in any
mode — the old root `NavBar` (701 lines, home strip + detail bar) and `TaskSidebar` are deleted, and
nothing may reintroduce a horizontal chrome bar. Contextual needs live in slim in-pane headers
(`SessionHeader.tsx` for sessions, `WikiTopBar` for wiki screens, `AppDetailHeader` for apps), never
in global chrome. This file is the rail's authority: anatomy, contracts, and the paid-for lessons.

## Anatomy (top → bottom)

1. **Header** — BrandMark + wordmark + version. Click → `/`.
2. **Product switcher** — four action rows (Tasks `/` · Wiki `/wiki` · Search `/search` · Apps
   `/apps`). Selecting navigates to the product landing AND scopes section 3. Exactly one row
   carries `aria-current="page"`; a route that maps to no product (`/settings`) marks none — the
   rail still renders (never strand a route without primary nav).
3. **Scoped section** (the one scrollable zone) — **the contents of the current scope**, which is
   recents for a product and the FACET LIST for Settings. Read the zone as "what is inside where I
   am", not as "recents": that is what lets `/settings` keep its navigation in the rail instead of
   showing another product's recents while the user is somewhere else. Each section is
   SELF-CONTAINED: calls its own hooks (TanStack cache dedupes by queryKey), navigates via wouter
   directly. `AppLayout` threads zero data props. This is the TaskSidebar convention, inherited
   deliberately.
4. **Pinned footer — ONE bordered zone** (`RailFooter`). The account cluster and the notification
   feed share a single enclosure: the account menu (avatar → EXACTLY four rows: Settings ·
   Documentation · GitHub · theme toggle), then an icon-only notification bell
   (`NotificationBell`, count badge → the shared `NotificationPanel`), then the Settings gear.
   The menu is not a page index — adding a destination here is the smell; add a facet or a rail
   affordance instead. **The bell replaced a full-width "Notifications" row**: collapsing the feed
   to an icon beside the gear reclaimed a whole row of vertical space, which is why the two former
   zones are now one. Expanded/mobile lay the cluster out as one horizontal row
   (`[account · bell · gear]`); collapsed stacks the bell above the avatar in the 48px rail, and
   the gear is absent there (Settings rides the avatar menu).

### Borders — the rail's two INTERNAL rules (the right edge is the content sheet's job)

The rail is a distinct canvas, so its INTERNAL zone boundaries have to be *stated*; a rail whose
zones dissolve into each other has no anatomy a user can read. Two rules, both on `--rail-border`:

| Rule | Where |
|---|---|
| Above the product-scoped section | the section wrapper in `RailContent` |
| Above the footer | the single merged footer zone in `RailContent` |

**The rail draws NO right edge of its own.** The `<aside>` used to carry a `border-r`, but the
content area already renders as one rounded floating sheet (`rounded-lg` + `--border` hairline +
`--background`) flush against the rail on the `--rail-bg` canvas — and that sheet's own rounded edge,
hairline, and the `--rail-bg`→`--background` colour step (rail LIGHTER than the sheet in dark, DARKER
in light) are three separation cues already. A straight `border-r` on top of them was pure
redundancy fighting the rounded aesthetic — worst at the sheet's rounded corners, where a straight
rail line meets a curve. It was DELETED (verified live, both themes + collapsed: the boundary reads
clean as an organic colour transition, the rounded corner floats). Do not re-add it; if the seam
ever reads weak, strengthen the SHEET's own edge (its hairline/elevation), never redraw a rail line.
The mobile `Sheet` is the ONE exception — it keeps `--border-strong` because it floats over a scrim,
a different law (documented below).

**The two internal rules ride `--rail-border`, never `--border`.** `--border` carries an alpha tuned
for hairlines *inside* a card; against the rail canvas the same value reads as no line at all, which
is what made the zone dividers measure as present and look absent.

**RULES are full-bleed; FILLS are not — this rules-vs-fills split is the load-bearing distinction.**
A `border-t` reaches the rail edges when its element spans the full rail width, and — the key fact —
a border on the SAME element as horizontal padding STILL spans full width, because the border sits
OUTSIDE padding (padding only insets a border it is an ANCESTOR of). A row's FILL (its rounded
hover/active pill) is the opposite: it must NEVER touch the rail edges, because a pill kissing the
viewport reads broken. So every zone carries a small `RAIL_GUTTER` (`px-1`, 4px) that floats the
fills, placed where it insets fills but never rules:

- The product-switcher `nav` and the footer zone div hold `RAIL_GUTTER` DIRECTLY. Their `border-t`
  is on that same div, so it stays full-bleed while the rows inside float off the edges.
- The scoped-section wrapper stays padding-free (its own `border-t` is full-bleed), and the gutter
  lives INSIDE `RailSection`, on the rows wrapper. This is forced: the kit's `RailSectionHeader`
  dividers (the rules under "New task"/"All wikis") are CHILDREN of that section, so a `px` on any
  wrapper AROUND them insets every one from the edges (the earlier bug). The header sits OUTSIDE the
  gutter wrapper; only the rows float.

`RAIL_GUTTER` is 4px for a reason: combined with each row's own `RAIL_PAD_X` (8px) it lands the row
icons on the **12px column where the brand mark begins**, so every icon in the rail starts where the
Mewbo logo starts (the user's alignment law). Labelled sections then add `RAIL_INDENT` on top, so
their content — and its fill — steps further in. Collapsed keeps the same `px-1` on the switcher and
footer, which also holds the centered glyph column off the rail's own right edge. **Verify by
measuring rendered rects against the aside, not by eye:** row icon-left == brand-glyph-left (12px),
every fill gutter > 0, every rule span == `[0, rail width]`.

The desktop `<aside>` draws no border at all now (see above), so its `48px`/`272px` width is pure
content — the `--rail-w` offset contract below is exact with nothing to subtract. The mobile `Sheet`
keeps `--border-strong` on its edge: it floats over a scrim, where the stronger value is what
separates it from the dimmed page.

## Scoped sections — data contracts

| Scope | Source | Notes |
|---|---|---|
| Tasks | assistant-ui `ThreadListRoot`/`ThreadListItems` via `MewboRuntimeProvider` (rows off `threadIds`, NEVER `threadItems`) | `isDefaultVisibleOrigin` filter (user+channel) shared with `HomeView` via `utils/sessionOrigins.ts` so the two can't drift; cap 15 + "View all tasks" |
| Search | `useRecentSearchRuns()` → `GET /api/agentic_search/runs?limit=` (cross-workspace, newest-first; server clamps limit to 100) | row → `/search?ws=<>&run=<>`; hook degrades to empty list on error — no error residue in the rail |
| Wiki (gallery + pre-index flows) | `useWikiProjects()` | row → project via wiki `buildHref` ONLY (never hand-built paths; never assume a 'core' page id) |
| Wiki (INSIDE a wiki: `page`, `qa`, `graph`) | `useWikiPage(pageId, slug)` → the payload's own `nav` tree | rows navigate to another page via `buildHref`; the page's own ToC is NOT here (see below); "All wikis" stays above so a reader is never stranded |
| Apps | `useApps()` filtered `status !== "archived"` | row → app detail |
| Settings | `useConfig()` → `SettingsModel.groups()`, filtered by the shell's own visibility rule (`sections.length > 0 \|\| FACET_PANES[id]?.length`) | row → `?facet=<id>`; NOT a product — no switcher row, so nothing is `aria-current` in zone 2 |

### Settings facets — the three rules that keep the rail and the shell agreeing

Zone 3 on `/settings` renders the facet list, so the Settings page does not have to spend its own
width on a second nav column. Both surfaces write the same `?facet=` key, which is what makes the
following load-bearing rather than stylistic:

- **Read the facet list from the model, never a copy.** `settings/facets.ts` owns the ids, titles,
  order and icon NAMES; the rail reads them through `SettingsModel.groups()`. A hand-kept list in
  the rail goes stale the moment a facet is added, and this repo has already paid for that class of
  bug once (a section whose `x-group` had no matching `FacetId` vanished silently into the fallback
  facet). The one thing the rail does copy is the `iconName` → lucide-component map, because
  `facets.ts` is deliberately React-free and the shell's map is private to it; an unmapped name
  degrades to `Settings2` rather than breaking the row. **Exporting one shared map from `settings/`
  would remove even that** — worth doing next time that directory is open.
- **Offer only the facets the shell considers VISIBLE** (has schema sections OR registered panes).
  The shell silently redirects an unknown or empty facet to the first visible one, so a row for an
  invisible facet would look like it did nothing. `SettingsModel.visibleGroups(paneCounts)` now owns
  that predicate and the shell reads it; the rail still RE-DERIVES the same
  `sections.length > 0 || FACET_PANES[id]?.length` filter inline (`settingsSection.tsx`) and should
  adopt the shared method next time that file is open — one predicate, two callers.
- **Resolve "current" the way the shell resolves it, not from the param alone.** With no `?facet=`
  the shell lands on the first visible facet, so a rail that only reads the param marks nothing
  current on the first paint of `/settings`. And a facet click must rebuild the query from `prev`
  and `.set()` only `facet` — the Automation pane is deep-linked as `?facet=automation&session=<id>`,
  and rebuilding the query from scratch drops the session.

**The shell treats `?facet=` as the source of truth, end to end.** `SettingsView` derives
`activeFacet` DIRECTLY from the param every render (`visibleGroups.some(g => g.id === facetParam) ?
facetParam : visibleGroups[0]?.id`) — there is no local `activeFacet` state, no seed-once effect to
fall out of sync with the URL, and the shell no longer writes the param at all (its own facet sidebar
and mobile Sheet are gone; the rail is the sole facet nav). So a facet written by the rail, or by any
deep link, moves the URL, the rail highlight AND the rendered pane together — the earlier seed-once
state that let them diverge is deleted.

### Wiki page tree — what belongs in the rail, and what deliberately does not

**The rail carries the PAGE TREE, never the table of contents — the distinction is the whole point.**
The rail is SITE navigation: "move to another page". A table of contents is INTRA-DOCUMENT
navigation and belongs beside the document it indexes, on the page's own right side. An earlier
revision had these two swapped, which read as a second left rail full of headings while the way to
another page sat elsewhere. If a future change wants something in zone 3, ask which of the two kinds
of navigation it is; that answers where it goes.

- **The entries are cached data, not a new query.** `nav` ships ON the page payload (it is the same
  tree for every page in a wiki), so the section calls the SAME `useWikiPage(pageId, slug)` query the
  page itself populated and the TanStack cache dedupes it — no second request, no props, no context.
  Reach for a context only if a surface genuinely cannot be re-derived from cached data; here nothing
  had to cross a boundary at all. The superseded ToC section rode this identical seam, which is what
  made the swap cheap.
- ⚠️ **`nav` is only ever returned ATTACHED to a page fetch — there is no project-level page-tree
  endpoint.** So a route that is inside a wiki without naming a page needs some page to key the fetch
  off. That is what `landingPageId` is for (validated to exist at finalize; already what `WikiScreen`
  uses for repo-level links), and the fallback is free because `useWikiProjectBySlug` is a `select`
  over the same `["wiki","projects"]` query this section already reads. **Keep the fetch key separate
  from `activePageId`:** on the graph route the tree is fetched via the landing page, but the reader
  is not on it, so nothing is marked current rather than highlighting a row they are not looking at.
- **Which routes get the tree, deliberately.** `page` and `qa` both name a page; `graph` names only a
  project and takes the fallback. The gallery and the pre-index flows (`welcome`, `indexing`) keep
  the project list — there is no page tree yet, and on the gallery the cross-project list genuinely
  IS the contents of the current context. Rendering an empty section on those routes would have been
  the wrong kind of consistency.
- **Section vs page is a STRUCTURAL test, not a positional one.** A row gets the folder glyph when
  some other entry names it as `parent`, and the page glyph otherwise. Keying off `lvl === 1` instead
  would brand every top-level leaf a section — a wiki's landing page and changelog are level 1 and
  have no children.
- **This is the one list that opts INTO leading icons.** Recents deliberately have none (the slot
  indents titles past the section header and breaks the one-column read). A page tree is
  heterogeneous, so the glyph carries a distinction a list of same-shaped recents does not have to
  make — which is why it goes through `RailRow`'s optional `leading` slot rather than becoming
  the default for every product.
- **Depth is indentation plus weight, never a smaller font.** `RailRow`'s `depth` prop steps
  levels 2 and 3 in and gives only level 1 `font-medium`. The in-page nav it replaced dropped level 3
  to `text-xs`; such steps bottom out at an unreadable leaf and break the three-adjacent-steps rule
  for the surface. Long labels truncate — the rail's width is fixed.
- **"All wikis" sits above both branches, with a rule beneath it.** A reader deep in a page must
  never need the browser's Back button to reach the rest of the product, and the divider (on
  `--rail-border`, like every other rail rule) keeps that escape hatch reading as its own zone rather
  than the first item of the list below it.

Tasks + Search recents share ONE date-bucket util (`utils/dateBuckets.ts`: Today / Previous 7 days /
Older, empty buckets dropped, `updatedAt || createdAt`, unparseable sorts Older). Don't fork it.

**The duplicate-list trap:** on `/` the landing IS the full task list, so the Tasks section
collapses to just "New task" there. Rendering recents on `/` doubles every title and breaks the App
smoke test — this was a documented TaskSidebar-era failure; the guard is deliberate.

## Design language (Aura-converged, web-translated)

Ported from the Aura drawer (`apps/mewbo_aura` — the reference implementation): flush-left compact
recents with NO leading icons (an icon slot would indent titles past the section header and break
the one-column read); trailing 8px `--primary` liveness dot gated on `running` (the wire's real
signal — never a derived status), trailing so liveness can never reintroduce the indent; full-row
selection fill; hairline dividers between zones; pinned footer.

**Deliberate divergences from Aura** (translation, not drift):
- Selection radius is `rounded-lg` (8px), NOT Aura's stadium pill — `rounded-full` is reserved for
  state containers by the console shape law. What transfers is the full-row fill + dedicated token.
- Desktop is a persistent collapsible aside, not Aura's modal overlay — only the row anatomy,
  density, grouping, and color layering port; the open/close mechanics do not.

**Tokens:** `--rail-bg` (rail canvas — a distinct layer, the Aura `surfaceDrawer` lesson: paint the
rail the page background and the selection layering dies), `--rail-selected` (row fill, the
`surfaceSelected` analog) and `--rail-border` (every rule the rail draws — see "Borders" above).
All three defined in `:root` AND `.light`. The app body paints `--rail-bg`;
the content area floats as ONE rounded inset sheet (`rounded-lg` + hairline + `--background`,
wrapper `p-2 md:pl-0`) — the inset sheet IS the paradigm statement; don't flatten it.

## Type & density ramp

**One size for every word.** Every piece of READING text in the rail renders at `text-sm` (13px) —
action rows, recents, section headers, date subheaders, timestamps and supporting lines alike. Sizes
still come from the console's named scale (**never** an arbitrary `text-[<n>px]`, which the
console-wide guard test rejects); the rail simply spends ONE step of it. Hierarchy is carried by
COLOUR, by INDENTATION (`depth`) and by a three-level WEIGHT ramp (`font-light` label · `font-normal`
row · `font-medium` current) — never by a size step, which at 1px reads as noise rather than rank and
bottoms out at an unreadable leaf.

| Element | Height | Size | Weight |
|---|---|---|---|
| Action / product rows (incl. the footer's account-menu row) | `h-8` | `text-sm` | `font-normal`, `font-medium` only when current |
| Recents | `h-7` | `text-sm` | `font-normal` |
| Section headers ("Recents", "Installed") | — | `text-sm` | `font-light` (300) — a non-interactive label, the LIGHTEST tier so it recedes below the rows it heads |
| Date subheaders, timestamps, supporting lines | — | `text-sm` | `font-light` subheaders; `font-normal` inline metadata |
| Numeric state pill (count pip · unread badge · version chip) — the SOLE exception | — | `text-2xs tabular-nums` | — |

**The colour ladder carries the hierarchy the size step used to.** The heading/label tiers
(`RailSectionHeader`, `RailDateGroup`, and the instrument rail's `InstrumentSection` heading) recede
onto the dedicated per-theme `--rail-heading` token, tuned to sit JUST ABOVE the 4.5:1 AA floor so it
recedes below `--muted-foreground` without ever dropping under AA — darker than muted in dark (5.33:1
on `--rail-bg`), lighter than muted in light (4.67:1 on `--rail-bg`, its worst surface). Both
directions mean the same thing: recede. It is calibrated to that floor, not to taste, so do NOT
"harmonize" it back toward `--muted-foreground` — the gap IS the heading↔metadata hierarchy. Metadata
rests on the FULL `--muted-foreground`; identity and the current destination take `--foreground`. A
word that reads as a word is `text-sm`; the numeric state-pill exception exists
only because a 13px numeral balloons a tiny overlay pill, and every such site is commented as the
exception. Words are never pills.

**EVERY icon and label begins on ONE base content column — 12px, where the brand mark begins — and
indentation only ever steps in AFTER it (the user's alignment law).** The 12px column
(`RAIL_CONTENT_START`, a derived `RAIL_GUTTER` 4px + `RAIL_PAD_X` 8px) is where the brand mark, the
product icons, every action row's icon, AND every header-tier label — section headers and date
buckets — begin. A ROW reaches it via the gutter (its zone's wrapper) plus its own padding; the
header/date-group tiers sit OUTSIDE the gutter wrapper (full-width, so their `border-t` stays
full-bleed), so they'd otherwise begin at the bare 8px `RAIL_PAD_X` — LEFT of the rows they head,
which is the "header icon closer to the edge than its rows" defect. `RAIL_CONTENT_START` is the fix:
nothing starts left of 12px.

**Indentation is the third hierarchy channel, after colour and weight, and it FOLLOWS the base
column.** A LABELLED section's leaf ROWS step one `RAIL_INDENT` (`pl-1.5`, 6px; deliberately below the
8px base inset, "visible but minor") further in, landing at **18px** = gutter 4 + pad 8 + indent 6.
Its date-bucket LABELS do NOT indent — they are header-tier, so they stay on the 12px column with the
section header (a date bucket renders inside the `RAIL_INDENT` wrapper with its rows, so its label
alone counteracts that one step with `-ml-1.5`, mirroring `RAIL_INDENT`; the two change together).
The rationale is accessibility: the indent makes a row's membership in its section legible at a
glance. Three rules keep it DRY: `RAIL_INDENT` is **defined once** (`rows.tsx`) and applied on **one
wrapper per rail** — the inner children wrapper that `RailSection` nests inside its `RAIL_GUTTER`
wrapper (the instrument rail's section body nests the same way), never per child; it applies **only
under a heading**, so an unlabelled section (a lone action row, the product switcher) takes the
gutter but no indent and stays on the 12px column; and `RailRow`'s `depth` composes its `pl-4`/`pl-6`
ON TOP of the indent for nested trees. The header is OUTSIDE both wrappers, so its full-bleed
`border-t` is untouched — only the leaf rows beneath it move.

**Glyphs step down with their label.** Rows carry `size-4`; the two LABEL tiers — `RailSectionHeader`
and `RailDateGroup` — carry `size-3.5` and inherit the label's muted colour. A header is a label, not
a target, so its icon should recede with its text rather than compete with the row glyphs beneath it.
Pick a glyph that MEANS something (a clock for a date bucket, a repo glyph for indexed projects); a
decorative one costs the same space and teaches nothing.

**Header padding is `pb-2` / `pb-1.5`, not `pb-1`.** At `pb-1` a header collided with the first row
under it and read as part of the list instead of introducing it. This spends back a little of what
the `h-8`/`h-7` rows reclaimed, which is the right trade: the density complaint was about the four
product buttons, not about section rhythm. The switcher's own height is unchanged.

**Weight is the primary hierarchy channel now that size is uniform, spent on a strict three-level
ramp.** `font-light` (300) is a NON-INTERACTIVE label — a section header, a date bucket, the
instrument-rail section heading — so it sits BELOW the rows rather than above them. `font-normal`
(400) is the resting state for every row and every piece of inline metadata. `font-medium` (500)
marks ONLY the current destination or an identity. `font-semibold` belongs to page headlines
(ProductHero) and never appears in the rail. This ramp is the fix for a real complaint: headings
that carried `font-medium` read as heavier than — and so like clickable siblings of — the rows
beneath them; a label must be the lightest thing in its zone, not the boldest.

**Non-interactive labels carry NO hover or active styling** — that is precisely what made them read
as buttons. `RailSectionHeader` and `RailDateGroup` are plain `<div>`s with none. The one label that
sits ON an interactive control is the instrument rail's `InstrumentSection` heading (its whole row is
a collapse trigger): the LABEL itself takes no hover promotion, and the collapse affordance lives on
the row tint + the rotating chevron, not on the word.

⚠️ **`font-light` at 13px is legible ONLY on a colour that clears 4.5:1**, which is exactly why the
three label tiers ride `--rail-heading` (5.33:1 dark / 4.67:1 light on `--rail-bg`) rather than a
lighter tone — it clears AA while still receding below `--muted-foreground`. If 300 ever reads as
frail, the fix is a DARKER token, never dropping the rows' own weight distinction — the ramp's three
levels are load-bearing.

**Hit targets, not type size, set the density floor.** 32px and 28px both clear the 24px AA
minimum, which is what licenses the tighter rows; do not shrink either further without re-checking
that floor.

⚠️ **The recents row has a second CONSUMER outside this directory — it must IMPORT, never re-spell.**
Tasks recents have to be the assistant-ui thread-list primitive to get click-to-switch and the active
tint, so `assistant-ui/thread-list.tsx` builds its own element rather than rendering `RailRow`.
That makes it a hand-kept copy by construction, and nothing fails when a copy drifts — which is
precisely how its row height came to disagree after the row moved to `h-7`.

So `rows.tsx` EXPORTS the vocabulary — `railRowCls` (geometry, spacing, resting type) and
`FOCUS_RING` — and that file composes them. One definition, so the next density change propagates
instead of diverging silently. **Adding a class to the row shell means editing `railRowCls`, not a
consumer.** Two divergences there are legitimate and should stay: colours split across Root/Trigger
(the primitive's structure — the resolved values are identical), and a flat `font-normal` for Tasks,
which pass no `depth`. Colour is deliberately excluded from `railRowCls` for that first reason.

⚠️ **The focus ring uses the UNDILUTED `--ring`, and this is not a style preference.** A `/50` on it
measured about 1.7:1, well under the 3:1 WCAG 2.4.11 asks of a focus indicator — the one affordance a
keyboard user cannot work around. It survived every automated check because the console's contrast
guard is scoped to `text-` position, so `ring-` is exempt by design; nothing will catch a
reintroduction. **If the ring ever reads too heavy at this density, spend ring WIDTH or offset.**
Thinning the colour is exactly what destroys the contrast. `FOCUS_RING` is the single definition —
`NavRail.tsx` and `sections.tsx` each used to keep their own copy of the string, which is how one
diluted spelling became three.

**Every zone label draws the rule above it.** `RailSectionHeader` carries `border-t` on
`--rail-border` by default, so the action row ("New task", "All wikis") is separated from the list it
introduces, in every product, from one place. It is self-limiting in the case that matters: Tasks on
`/` renders no header, so no orphan rule floats under a lone action row. `divider={false}` is for the
one section whose header OPENS the zone — Settings has no action row, so a rule there would double
the zone divider already above it. A per-section `border-b` was tried first and removed; it put the
decision in four files and would have drifted.

**Metadata uses the FULL `--muted-foreground`, never an alpha of it.** Date subheaders and recent
timestamps once rendered at `--muted-foreground/0.7`, which measures below the WCAG AA contrast
floor in both themes while the full token passes. Now that colour is the ONLY rank channel left for
metadata — the size step is gone — diluting it is doubly wrong: a lighter metadata tone would erase
the one distinction that survives, not soften a size step that no longer exists. The receding move
that DOES belong to a heading rides its own token — `--rail-heading`, per-theme tuned to sit just
above the 4.5:1 AA floor (receded, never under it) — so a heading recedes there while metadata holds
the full `--muted-foreground`, and the two tiers read apart without either dropping under AA.

**The console-wide adjacency rule — at most three ADJACENT scale steps per surface — still stands
globally; the rail simply now spends ONE step, which trivially satisfies it.** Do not read the rail's
single size as licence to loosen the global law elsewhere. And the worked example is a POPOVER, not
the rail: the account MENU may keep its own three-step ramp (`text-sm` names · `text-xs` supporting
lines · `text-2xs` chips) — do NOT "fix" it to one size, and do NOT cite it as the rail's pattern.
Inside the rail, every word is `text-sm` and hierarchy is colour + indentation + sparse weight.

Icons: lucide only, `size-4` for rows and menu items / `size-5` brand, `gap-3` action rows / `gap-2`
elsewhere. `tabular-nums` on any live counter. ⚠️ **A size utility on a menu icon is inert** —
`DropdownMenuItem` styles its children through `[&>svg]:size-4`, a descendant selector that
outranks the child's own `size-*` class, so a hand-written `size-3.5` there changes nothing and
just misleads the next reader. Match the item's own size or restyle the item.

## State & responsiveness

- ONE persisted state: `mewbo:rail` = `"expanded" | "collapsed"` (desktop only). Legacy
  `mewbo:sidebar-open` is read once as a migration fallback, then only `mewbo:rail` is written.
- Mobile (`useIsMobile()`, 768px — the JS-hook strategy, not Tailwind breakpoints, for this
  decision): rail becomes a `Sheet side="left"` rendering the SAME rail content 1:1, never
  persisted, default closed, auto-closes on every navigation. Entry points: floating hamburger on
  landings (`variant="neutral"`, not ghost — a ghost button is invisible over varied content) and
  the `SessionHeader` hamburger, both via the `RailControls` context from `AppLayout`.
- Collapsed desktop = 48px icon rail (brand, product icons, bell, avatar; labels hidden;
  `Tooltip side="right"`).
  - **One centerline — the collapsed column is a single vertical rhythm.** Every glyph below the
    brand sits in the SAME hit-target box: the kit's `railActionRowCls` + `justify-center px-0`
    (`h-8 w-full`, centered). The product switcher uses it via `RailActionRow`; the footer's bell and
    avatar COMPOSE the same `railActionRowCls` box rather than hand-rolling their own, so nothing
    lands off-axis. Glyphs are `size-4` — the collapsed avatar steps down from its expanded `size-6`
    to `size-4` so it matches the bell instead of dominating; the brand keeps its documented `size-5`
    on the same centerline. The unread badge is `absolute` over the bell's icon corner, so it never
    widens the flex item or knocks the bell off-center. `navRailCollapsedSymmetry.test.tsx` guards
    the box parity so a re-hand-rolled footer icon fails a test instead of shipping a crooked rail.

## `--rail-w` — the offset contract for viewport-fixed satellites

`AppLayout` sets `--rail-w` on its root: `0px` mobile / `48px` collapsed / `272px` expanded.
**Any `position: fixed` element that centers or anchors relative to the viewport must offset by
`var(--rail-w, 0px)`** — the fallback keeps it correct if the var is ever absent. Canonical
consumer:
- `QADock`: `left-[calc((100vw+var(--rail-w,0px))/2)]` AND `w-[min(720px,calc(100vw-var(--rail-w,0px)-2rem))]`
  — the width cap must shrink too; shifting only the position pushes the right edge off-screen on
  narrow viewports with the rail expanded. This second-order bug is why both halves are law.

**Prefer escaping the rule over obeying it:** `TurnScroller` originally carried the `--rail-w`
offset as a fixed element, then dropped it entirely by re-anchoring `absolute` inside `.conv-scroll`
(relative + overflow-hidden, already spanning header-to-composer beside the rail) — containment and
offset both hold by construction, no viewport math. If a satellite can anchor inside a pane's
positioning context instead of the viewport, do that; `--rail-w` is for the elements that genuinely
cannot (viewport-centered overlays like QADock).

## Z-index

The rail has NO explicit z-index on desktop — it is a normal-flow aside. The deleted NavBar's
`z-50` was the app's highest; do not re-create it. `SessionHeader` is `z-20` in-pane, below Radix
portals. Adding any z-index to the rail must be justified against the full stacking chain
(console CLAUDE.md → "Z-index stacking contexts").

## Change discipline

- A new product row = a `products.ts` entry (component-free module: `PRODUCTS`, `Product`,
  `ActiveProduct`) + a section component honoring the self-contained contract + an `activeProduct`
  arm in `App.tsx`. Nothing else.
- **A scope is not automatically a product.** `ActiveProduct` is `Product | "settings" | null`:
  Settings scopes zone 3 without earning a switcher row, because the four-product switcher and the
  four-row avatar menu are both invariants and Settings is reached from that menu. Widening the
  union is how a route joins zone 3 without joining the switcher — prefer it to a fifth product
  row, and keep `PRODUCTS` the sole source of what the switcher renders.
- **A section whose deps live behind a lazy route loads lazily here too.** The rail mounts on every
  route, so the settings arm is a `React.lazy` split (`settingsSection.tsx`) with its own
  `<Suspense>`: importing `SettingsModel` and the pane registry eagerly would pull the whole
  Settings machinery out of its chunk and into the bundle every route pays for.
- Rail row primitives (`rows.tsx`) are the ONLY row chrome — a product section that hand-rolls its
  row markup is a review reject.
- Preserve module-scope subcomponents (Radix open-state survives parent re-renders — the NavBar-era
  lesson, still load-bearing in `SessionHeader` and the rail footer).
- The rail shows NO error residue: a failed section query renders its empty state, never a red
  banner (the compact-design no-error-residue law applied to chrome).
