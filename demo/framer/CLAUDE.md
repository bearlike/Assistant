> ↑ [demo/CLAUDE.md](../CLAUDE.md) · [root](../../CLAUDE.md)

# Demo framer — the window-on-wallpaper compositor

Scope: `demo/framer/` (`mewbo_demo_framer`), a standalone uv workspace member.
Composites `docs/assets/img-src/*` into `docs/assets/img/*` — the tree the docs
site and the README embed. Run it via `uv run mewbo-demo-frame` or
`python -m mewbo_demo_framer` from anywhere; both default to those two paths,
resolved from the package's own `__file__` rather than the caller's cwd.

## Why source-to-derived, not in-place

⚠️ **`img-src` holds ONLY what the demo project generates — nothing else, ever.**
It is exactly the set `shots.ts` declares: one file per capture this stack can
re-run at will. `img` holds everything a doc references: those derived artifacts
PLUS the hand-maintained static assets this package never touches.

That is a rule about MAINTENANCE, not about file type. A hand-captured image is
not a source just because it happens to have been composited once — nothing
regenerates it, so it IS the artifact, and it lives in `img` alone, absent from
`artifacts.json`. This covers the Aura phone shots, the CLI terminal rasters, the
already-composited hero images, the animated GIFs and the mail capture.

**When adding an artifact, ask what produces it. If the answer is anything other
than "a spec in this repo", it does not go in `img-src`.** A static kept there
would commit the same bytes twice and invite someone to "fix" a published image
by editing a copy nothing reads.

**The accepted cost, stated so nobody rediscovers it as a bug:** the frozen
composites do NOT follow a `FrameStyle` change. Re-tuning the frame updates the
23 generated artifacts and leaves the hand-maintained ones on the old geometry
until someone re-composites them by hand. That is the price of keeping this
directory honest, and it was chosen deliberately over carrying unregenerable
inputs here.

The transform is one-way for two reasons:

- **The frame style is re-tunable without re-capturing.** Corner radius, shadow
  softness, padding — all of it lives in `FrameStyle`, not in the pixels. Bump a
  ratio and re-run `mewbo-demo-frame`; nobody re-runs Playwright or re-does a
  hand capture to see the new chrome.
- **The composite is idempotent by construction.** Because `img` is fully
  derived and never hand-edited, re-running the framer twice with no source
  change produces byte-identical output (pinned by
  `test_render_is_deterministic_across_independent_runs`) — the same
  zero-diff property `demo/CLAUDE.md`'s screenshot gate depends on, one layer
  further downstream.

## The classification rule

`artifacts.json` is the trust boundary between a human decision and the
renderer. Every file in `img-src` needs exactly one entry, and
`FrameRunner._audit` fails in **both** directions before anything renders: a
source file with no manifest entry is a `ValueError` naming the file (nobody
decided what it is), and a manifest entry with no backing file is a
`FileNotFoundError` naming the file (the manifest outlived its artifact). This
is deliberate, not an oversight to relax — defaulting an unclassified capture
either way (frame it, or skip it) would silently publish the wrong picture, and
the whole point of the split is that classification is a judgement call a
machine cannot make:

- **`window`** — a capture contained, rounded and centred on a named canvas. A
  whole window and a single card both qualify; what the entry decides is that
  this artifact GETS a canvas, and which canvas decides what sits behind it.
- **`passthrough`** — copied byte-for-byte, untouched. Covers: an animated GIF
  (the compositor is a still-image pipeline), a Rich SVG terminal export
  (already draws its own window chrome — framing it again double-frames), every
  capture the owner composited by hand before this package existed (already
  16:9 on the same wallpaper; re-compositing would just re-derive what's
  already correct), and a source so far from every canvas's ratio that
  containing it would shrink it past reading. Passthrough is a copy rather than
  a skip so one invariant holds for every published file without exception:
  everything under `img` was produced from `img-src` by this manifest, none of
  it by hand after the fact.

⚠️ **"An element crop is passthrough" WAS the rule and no longer is.** The four
tool-render closeups shipped at their raw crop sizes, so a docs page mixed 16:9
composites with bare 0.50-to-1.01 rasters. Three are now `window` on the
`closeup` canvas. The reason the old rule gave — a window frame around one card
is nonsense — was right about the WALLPAPER and wrong about the CANVAS: what
reads as nonsense is a card floating on a desktop photograph it never sat on,
not a card sitting on the surface it was lifted off.

**Phone-portrait Aura captures are `passthrough`, and that is an open question
rather than a settled rule.** Contained on a 16:9 canvas a phone occupies about
a quarter of the width, which costs more legibility at the size the docs render
these than the consistency buys. The existing two-phone banner GIF is the shape
of the likely answer — a multi-device composition is a different artifact, not a
reframing of this one. Tracked; do not quietly flip them to `window`.

## Three canvases — 16:9 for web, 4:3 for portrait, 4:3 matte for closeups

`artifacts.json` declares NAMED canvases and each `window` artifact says which
one it belongs on. A canvas is a whole `FrameStyle`, not just a size, so two
canvases may share a geometry and differ only in treatment:

| Canvas | Size | Backdrop | Shadow | For |
|---|---|---|---|---|
| `wide` | 2400x1350 (16:9) | wallpaper | yes | every web capture — console, wiki, search, settings, CLI |
| `portrait` | 1800x1350 (4:3) | wallpaper | yes | Aura Android phone captures |
| `closeup` | 1800x1350 (4:3) | solid `#161513` | **no** | element crops of one tool-render card |

**All three are authored at the same HEIGHT.** That is the reason 4:3 is
1800x1350 rather than some other 4:3 size: a phone artifact, a closeup and a
browser artifact placed in the same docs page are then the same height, and
none reads as the odd one out.

The portrait canvas exists because contain-fitting a ~0.46-ratio phone into a
16:9 canvas leaves the phone about a quarter of the canvas width, and at the
width docs render these that is unreadable. A 4:3 canvas is squarer, so the
phone keeps far more of the frame while the composition still reads as a device
on a desktop wallpaper.

`closeup` shares that geometry and differs in style alone, because a card is
not a device: the same 4:3 shape suits it, and nothing else about the desktop
composition does.

## The backdrop is a discriminated union on `FrameStyle`

`FrameStyle.backdrop` is a `Backdrop` — `WallpaperBackdrop` or
`SolidBackdrop`, on a `kind` discriminator — and **the wallpaper is the
default**, so a canvas declaring no backdrop renders exactly what it rendered
before the field existed. Each variant owns its own `prepare(wallpaper, size)`,
so `WindowFramer.load` prepares whichever the style names and `frame()` never
branches: the backdrop is a canvas-sized image by the time a composite starts,
exactly as the cover-fitted wallpaper always was. **Do not add an `if` to
`frame()` for a third backdrop** — add a third union member with its own
`prepare`.

- `SolidBackdrop.color` is `#rrggbb`, validated AT DEFINITION by a pattern
  constraint. A malformed matte must be a `ValidationError` naming the manifest
  before anything renders, not a Pillow error partway through a run with half
  the artifacts already published.
- `SolidBackdrop.prepare` takes the wallpaper and ignores it. That unused
  argument is the point — it is what lets both variants answer one call.
- **The wallpaper is still decoded whatever a canvas declares**, so the trap
  below (a manifest always needs a real wallpaper file) survives a canvas
  moving to a solid colour, rather than turning into an error nobody expects
  the day some other canvas moves back.

### The matte is `#161513`, and it is not a choice this package made

That is the console's own `--background` token (`apps/mewbo_console/src/index.css`,
dark `40 6% 8%`, commented "docs warm carbon"). It is verifiable rather than
matched by eye: `mewbo-console-file-read-log.jpg` keeps a margin of parent pane
around its card, and `mewbo-console-ask-user-log.jpg` crops a whole conversation
column WITH its surrounding page — every border pixel and all four corner
regions of both sample exactly `(22, 21, 19)`. **Re-derive it from the token if
the console's dark background ever moves; do not re-pick it by eye.**

⚠️ **It is NOT true that every closeup was photographed on this surface, and
believing that will mislead the next person who checks.** The conversation
column sits on `--background`, but `TerminalCard` and `DiffCard` render inside
the workspace pane, whose body is `--surface-deep` (`WorkspacePanel.tsx` →
`bg-[hsl(var(--surface-deep))]`, `#0c0c0b`). Their crops are flush to the card
border, so **neither** parent surface appears in their pixels at all — sampling
their corners returns the card's own `--code-chrome` `#1b1a18` at the top and
`--code-body` `#0e0d0c` at the bottom, never a page background. This is an
occlusion, not a contradiction: two crops pin `--background` exactly, and the
other two cannot contradict it because they contain no evidence either way.
One matte still serves both, because `--surface-deep` and `--background` differ
by about 10 sRGB units at this luminance — under the JPEG noise already present
in these corners, and with no parent pixels in the crop there is no seam for it
to mismatch against.

### Why `closeup` has no shadow

`shadow_opacity: 0.0`, and this is a style parameter rather than a branch in
code precisely so the reason can be written down here instead of inferred from
a conditional. The shadow is black at 55%, and it earns that on a photograph:
it seats a window into a scene. A flat near-black matte has no scene. Over
`#161513` the same shadow lands around `(10, 9, 9)` — not a shadow, a dirty
smudge that makes the one thing this canvas is for (a clean, uniform matte)
non-uniform. The `ambient_*` contact layer scales off `shadow_opacity`, so
zeroing that one value drops both layers together and the mask composites to an
exact no-op.

### Why `closeup` has NO hairline separator, which was not obvious

A matte equal to a card's own surface would make the card edge vanish, and
`--background` and `--card` sit only 1.09:1 apart, so this looked likely.
Rendered, it does not happen, and the reason is that **the console already
draws the hairline.** Every one of these cards carries `border
border-[hsl(var(--border))]` on all four sides plus a coloured left accent
stripe — `border-l-[3px]` off `--success`/`--primary`/`--destructive` on
`TerminalCard`, `border-l-[2.5px] border-l-agent-4` on `DiffCard`, the
`LogEventCard` rail elsewhere. Adding a separator would double a line that is
already in the pixels. Reinforcing it, each crop also carries chrome departing
from the matte in both directions: a card header at `--card` (`#201f1d`,
lighter), a terminal or code body on the `--code-*` family (`#0e0d0c`, darker).

Checked at the 720px width the docs actually render these at, the card edge is
legible on all three. So there is no separator parameter — an unused knob with a
documented default is still a knob nothing needs. **If a future closeup crops a
borderless region, this is the paragraph that changes**, and the fix is a styled
parameter, never a literal.

### `mewbo-console-ask-user-log.jpg` is the one that did NOT move

It stays `passthrough`, for two independent reasons.

**It is not the kind of thing this canvas is for.** `closeup` mattes a
tool-render CARD. This crop is a whole conversation column holding three
question cards, their option lists and a notes box — a page region, not a card.
That distinction decides the case on its own, before any measurement.

**And the arithmetic is decisive anyway.** At 1640x3314 it is 0.50, the tallest
and widest source of the four. Contained on 4:3 it lands at 600x1212 —
**33.3% of the canvas width**, against 68% / 53% / 42% for the other three. The
column is authored at 820 CSS px, so in a 720px docs column it would render at
0.29x authored size where the raw file renders at 0.88x. State the baseline when
quoting the loss: **3.0x smaller than it renders today**, 3.4x smaller than 1:1,
and about 9x less text area either way. Its 11px option descriptions land near
3px. That artifact exists to document three question-card states and their
affordances, and none of them survive it.

No canvas size fixes this: on-page legibility depends on the source's share of
the canvas WIDTH, not on absolute pixels, and 0.50 into 1.333 is 37% by
arithmetic. The options are cropping (which `place` will never do) or splitting
the capture into per-card crops (a capture change, not a compositing one).

⚠️ **The canvas is DECLARED, never derived from the source's aspect ratio.**
Auto-picking by measuring the input would silently move an artifact to another
shape the day a capture viewport changes — and with `portrait` and `closeup`
sharing a geometry, measuring could not tell them apart at all. It is a human
decision, the same as the treatment itself, and `resolve_canvases` refuses a
name the manifest does not define rather than falling back to the wide default.

⚠️ **A 4:3 artifact must not end up in a `.ms-shots` carousel.**
`docs/assets/css/shots-16x9.css` pins that box to 16/9 for the whole site, so a
4:3 image there letterboxes — the same bug that override was written to fix,
inverted. Adding an Aura capture OR a `closeup` to a carousel needs a per-slide
modifier, not a change to the global rule. The four tool-render closeups are
safe today because `features-builtin-tools.md` and `web/sessions.md` embed them
as plain centred `<img>` at `max-width: 720px`, not as carousel slides.

## The measured geometry

`FrameStyle`'s defaults are ratios of canvas size, not raw pixels, so one style
renders identically at any canvas size — but the ratios themselves are not
arbitrary. They were measured off the owner's own hand-made reference
composites (`mewbo-apps-01-detail.png` and its `passthrough` siblings in
`artifacts.json`) so a new capture's frame matches the ones composited by hand
before this package existed. At the default `2400x1350` canvas the ratios work
out to:

| Ratio | Value | ≈ pixels at 2400×1350 |
|---|---|---|
| `padding_y_ratio` | 0.051 | 69px top/bottom inset |
| `padding_x_min_ratio` | 0.060 | 144px horizontal floor |
| `radius_ratio` | 0.0104 | 25px corner radius |
| `shadow_blur_ratio` | 0.0292 | 70px Gaussian sigma |
| `shadow_spread_ratio` | 0.0042 | 10px silhouette growth |
| `shadow_offset_y_ratio` | 0.0100 | 14px downward displacement |

**Do not round these off.** They are not designed numbers with a clean
justification each — they are a fit to a reference image, and "cleaning up" the
0.0104 to 0.01 or the 0.0292 to 0.03 measurably shifts the frame off the
reference it was matched to. If the reference composites are ever re-measured,
re-derive the whole table from them, in one change, with the new references
named.

Every ratio but one is a fraction of `canvas_width` — corner radius, both
shadow blur/spread, and the horizontal padding floor all scale off width alone;
only `shadow_offset_y_ratio` and the vertical padding read `canvas_height`.
That is deliberate, not an oversight: every canvas is authored at the same
1350px height and only two widths, so keeping every non-vertical ratio on one
axis is what makes `FrameStyle(canvas_width=…, canvas_height=…)` overrides (as
the test suite uses, for a canvas small enough to render fast) scale the whole
frame consistently rather than needing two independently-tuned ratio sets. The
visible consequence of reading width alone is that a 4:3 canvas gets a
proportionally smaller radius and a tighter horizontal floor than a 16:9 one —
correct, since both are fractions of the shorter dimension there.

## Why the shadow blurs the silhouette, not the bounding box

`WindowFramer._shadow_mask` blurs the window's own rounded-rectangle alpha
mask, then pastes and blurs *that*, rather than blurring a plain rectangle the
size of the window. That is what CSS `filter: drop-shadow()` does in a browser,
and what `box-shadow` does not: a box shadow's blur radius still emanates from
the rectangular bounding box, so it leaks square corners out from behind a
rounded window. Blurring the silhouette instead means the shadow is rounded
everywhere the window is rounded, with no square corner ever visible past the
radius. The two-layer composite (`cast over contact`, `_blurred_silhouette`
called twice at different sigma/opacity/offset) exists because a single cast
shadow alone reads as the window floating above the wallpaper; the tighter,
zero-offset contact layer is what seats it.

## The byte-determinism obligation

`_encode` in `manifest.py` pins JPEG (`quality=90, subsampling=0,
optimize=True`) and PNG (`optimize=True, compress_level=9`) explicitly rather
than taking Pillow's defaults, because the demo pipeline's zero-diff gate
diffs *committed bytes* across runs — a default that drifts with a Pillow
version bump would read as a spurious UI change in review. Any new encoder
knob touching output bytes (a different `optimize`, a Pillow major bump that
changes its own defaults) needs the same treatment: pin it explicitly, and
re-render + review the byte diff of every affected `img` file in the same
change, never folded into an unrelated one.

## Traps (each verified directly)

| Trap | Consequence | Verified by |
|---|---|---|
| `FrameRunner.run()` unconditionally loads the wallpaper before touching any artifact — even a manifest with **zero** `window` entries, or one whose every canvas is a `SolidBackdrop`, still needs a real wallpaper file at the declared path | an all-`passthrough` manifest still raises a raw `FileNotFoundError` from `Image.open` if the wallpaper is missing, which reads as unrelated to the (passthrough-only) artifacts you're actually rendering | ran `FrameRunner.run()` against an all-passthrough manifest pointing at a nonexistent wallpaper path — `FileNotFoundError` on the wallpaper path, not a manifest/audit error. `WindowFramer.load` decodes it before consulting `style.backdrop`, deliberately, so a canvas moving to a solid colour cannot quietly change when this fires |
| `_audit`'s two failure directions are sequential, not simultaneous: it raises on any **undeclared** source file before it ever checks for a **missing** one | if a run has both problems at once, only the `ValueError` (undeclared file) surfaces; the `FileNotFoundError` (missing source) stays hidden until the first is fixed and you re-run | ran `_audit` against a manifest with one undeclared source file AND one declared-but-missing file simultaneously — only the `ValueError` fired |
| Ruff's isort (`combine-as-imports` only, no explicit section overrides) sorts `mewbo_demo_framer`'s own `from` imports into the **same** third-party group as `PIL`/`pydantic`, case-insensitively, with straight `import x` first — so `from mewbo_demo_framer...` lands *before* `from PIL...`/`from pydantic...`, not because it's first-party but because `"mewbo_demo_framer" < "PIL" < "pydantic"` case-insensitively | hand-ordering imports "by feel" (stdlib → this-package's-own → third-party, with a blank line separating "ours" from "theirs") fails `ruff check`, and the fix looks backwards at a glance | `ruff check --diff` on a hand-ordered import block; applied the exact diff, re-ran clean |
| No `demo/**/tests/**` entry in `[tool.ruff.lint.per-file-ignores]` (unlike `tests/**` and `apps/*/tests/**`) | every test function under `demo/framer/tests/` needs a real docstring — skipping one is a lint failure, not a style nit | `ruff check demo/framer` flags a docstring-less test function; matches the existing convention in `demo/seeder/tests/` |
| ⚠️ **A narrow `uv sync` drops `mewbo-demo-framer` out of the shared venv** — it is in `dependency-groups.dev` now (alongside `mewbo-demo-seeder`), but root `[tool.uv] default-groups = []` means a sync naming no group requests no group, and `uv sync` is EXACT by default, so "not requested" reads as "remove" | every test then fails collection with `ModuleNotFoundError: No module named 'mewbo_demo_framer'` — which reads as a broken package, not a missing install | happened for real: a concurrent `uv sync` removed it mid-session. Re-sync with the full `uv sync --all-extras --all-groups`, or recover just this one with `uv pip install -e demo/framer --no-deps`. **`uv run` is not the culprit and never was** — it is INEXACT by default, so neither `uv run pytest demo/framer/tests` nor `make demo-frame`'s `uv run --package mewbo-demo-framer` removes anything; both were measured leaving the dev group and every extra in place. See root CLAUDE.md → "Running, testing, linting" |
| **`shots.ts`'s `IMG_DIR` points at `img-src`, not `img`** | point it back and a raw Playwright capture overwrites its own published artifact, silently un-framing whichever shots that run touched — while every spec stays green, because a spec asserts what it captured, never what got published | the constant carries the warning inline; `make demo-frame` restores the frame, so the symptom is "some artifacts lost their frame after a capture run" |
| **The docs theme pins the carousel box to a fixed aspect ratio** — `mkdocs-shadcn-mewbo` ships `.ms-shots img { aspect-ratio: 4/3; object-fit: contain }` | every 16:9 artifact letterboxes into ~25% dead space inside the theme's own bordered, rounded box: a visible frame inside a frame | `docs/assets/css/shots-16x9.css` overrides the box to 16/9 via `extra_css`. It is a bridge to delete once the theme ships 16:9 itself, not a place to add rules. Verified on the BUILT site: computed `aspect-ratio` and rendered box ratio both 1.77778 against a 2400x1350 natural size |
| `docs/assets/img-src/` sits inside the docs tree | every source would ship alongside its published artifact, doubling the built site | `mkdocs.yml` `exclude_docs` carries `assets/img-src/`. Verify by checking `site/assets/` after a build — `img-src` must be absent and `img` must hold every published file |
| A docs page may reference an artifact the manifest does not declare | the manifest speaks for the SOURCE tree, not for what a doc happens to link, so this is not an error and no gate catches it | when an expected image is missing from a page, check `artifacts.json` and `img-src` before suspecting the renderer |

## Framing a video — the same canvas, by hand

`mewbo-demo-frame-video SOURCE OUTPUT` (`video.py`, `VideoFramer`) puts a screen
recording on a framer canvas. Pass the source as the output to frame in place,
which is the normal case: the docs embed these files by name, so a rename would
break every `<video>` tag.

| Flag | Default | |
|---|---|---|
| `--canvas` | measured | a canvas name from `artifacts.json`; the default measures the source |
| `--zoom` | `1.0` | scales the window about the canvas centre, after the contain fit |
| `--crf` | `24` | libx264 quality, lower is larger |
| `--fps` | `30` | output frame rate |
| `--manifest` | this package's | where the wallpaper and the canvases come from |

**It is NOT part of `make demo-frame`, and that is the point.** These recordings
are hand-made captures that nothing in this repo regenerates — artifacts, not
sources, by the maintenance rule at the top of this file. So they never enter
`img-src` and they get no `artifacts.json` entry; the audit would fail on one if
they did. An operator runs this once per recording and looks at the result.
`mewbo-demo-frame` is a separate console script for the same reason: `make
demo-frame` invokes it bare, so a required subcommand would break it.

**The canvas IS measured here, and that does not contradict the manifest rule
above.** A manifest entry is a lasting classification that must not move when a
capture viewport changes; this is one person framing one file and looking at
what came out. Taller than wide takes `portrait`, anything else takes `wide` —
square counts as wide — and `--canvas` always wins. A name the manifest does not
define is refused by `FrameManifest.canvas`, never defaulted.

### Why it matches a framed still exactly

No geometry is re-derived. `VideoFramer.cover` renders **`WindowFramer.frame()`
inverted**: one canvas-sized RGBA layer holding the backdrop and the shadow, with
the rounded window punched out of its alpha. A still pastes its window ONTO the
plate through `window_mask`; here the same plate is painted OVER a moving one
through that mask complemented. The blend is identical either way —
`plate*(1-a) + window*a` — so both surfaces read one `FrameStyle`, one set of
measured ratios and one silhouette.

That inversion is also what keeps ffmpeg to **one** filter graph and one still
input: `scale` and `pad` place the frame at the rect `place()` computed, and a
single `overlay` paints the cover over it. `format=rgb` on that overlay is
load-bearing — the cover's corner pixels carry fractional alpha, and blending
those in a subsampled chroma space fringes exactly the rounded corners the frame
exists for.

`WindowFramer.plate()` and `window_mask()` are public for this one caller.
`frame()` is written in terms of both, so there is no second copy of the
composite to drift.

**Verified against the reference:** the hand-composited `mewbo-aura-01-chat.png`
measures a 559x1212 window at (620, 69), which is exactly
`FrameStyle(1800,1350).place((1440,3120))` — so the framed Chat recording lands
on the identical rect, and the wallpaper around it matches the still to within
1-2 sRGB units of codec noise.

### Traps this one has of its own

| Trap | Consequence | Rule |
|---|---|---|
| **`ffprobe` prints CODED dimensions, and a phone recording routinely carries a quarter-turn display matrix** (either the modern `side_data_list` rotation or the legacy `tags.rotate`) | the contain fit is computed against a sideways aspect, so the window is placed landscape and the phone renders squashed inside it | `measure()` reads both spellings and transposes on 90/270. ffmpeg's decoder auto-rotates before the filter graph, so every number downstream must be the DISPLAY pair. The two committed Aura recordings carry no rotation at all — their re-encode baked it in — so this guard is for the next capture, not for them |
| `overlay` crops silently | a `--zoom` past the canvas edge would publish a video with a slice of the phone missing and report success | `place()` refuses such a zoom rather than clamping it |
| `zoom` is video-only and deliberately absent from `FrameStyle` | — | the still pipeline's committed bytes are a zero-diff gate; a knob no artifact sets does not belong in the shared style |
| The output is written to a sibling `*.framing.mp4` and moved into place only on success | — | that is what makes framing in place safe; a failed encode leaves the original untouched |
| **Which way the file size moves depends on the canvas, not on the tool** | assuming either direction leads to a surprise | measured. A phone recording SHRINKS — the 4:3 canvas has fewer pixels than a 1440-wide phone frame, so the downscale more than pays for the backdrop: Chat 401,370 → 340,333 bytes, Overlay 1,951,188 → 1,127,619. A landscape recording GROWS, because the 16:9 canvas is larger than the source and the window is scaled UP: the CLI clip went 461,613 → 735,284 (+59%). Both are acceptable; a result past a couple of MB is not, and is a reason to re-tune the encode rather than ship. Audio is copied through, never re-encoded — and `-map 0:a?` is optional because the CLI recording has no audio stream at all |
| ⚠️ **A source wider than its canvas under-fills it, and that is correct** | it reads as a bug — the window does not reach the padding box, and the temptation is to crop or special-case the ratio | the CLI recording is 1920x934, a 2.06:1 ultrawide (downscaled from a 3426x1666 original). On the 16:9 canvas it binds on WIDTH, landing 2112x1027 at (144, 161): flush to the horizontal padding floor with 161px of backdrop above and below. The canvas is the constant; the source's own ratio is not. `place()` contains rather than crops for exactly this reason, and auto-detection picking `wide` for it is the right answer for the right reason |
| A framed video takes its canvas's ratio, so it inherits the carousel hazard above | a 4:3 video in a `.ms-shots` box letterboxes | the pages embed these as plain `<video>` with explicit `width`/`height`, not as carousel slides. The Aura pair is 1800x1350; the CLI clip is 2400x1350 |

## Testing

`demo/framer/tests/test_framer.py`, registered in the root `pyproject.toml`
`[tool.pytest.ini_options].testpaths`. Run with `uv run pytest
demo/framer/tests`. Sources are tiny Pillow-generated solids, not the real
multi-megabyte captures, except for the one test that loads the real
`artifacts.json` and calls the real `FrameRunner._audit` against the real
`docs/assets/img-src` tree — the test that catches a new capture landing
unclassified. Cover, at minimum: `FrameStyle.place()`'s pure geometry (aspect
preserved, centred, never exceeds the content box) with exact integers, not
approximations; the padding-floor behavior on a source much wider than the
canvas; the `extra="forbid"` boundary on every model; BOTH discriminated unions
parsing every variant and rejecting an unknown `treatment` / `kind`; both
`_audit` failure directions; byte-exact `passthrough` output; canvas-sized
`window` output; and render determinism across two independent output
directories, over a manifest carrying every treatment and every backdrop at
once.

`demo/framer/tests/test_video.py` covers the video path and runs **no ffmpeg**:
both subprocess seams are exercised by pointing `VideoFramer` at a stub
executable, which is why the two binaries are injectable fields. Its
load-bearing test composites `cover()` under a placed frame in Pillow and
asserts the result is byte-identical to `WindowFramer.frame()` — that is the
whole claim the module makes, pinned without decoding a video. Note what it does
NOT pin: ffmpeg's own rounding on antialiased corner pixels is its own, and only
looking at a rendered frame checks that.

The backdrop's own obligations: a malformed `color` raises `ValidationError`
(parametrized over the ways a colour goes wrong — no hash, wrong length, the
three-digit CSS shorthand, a colour NAME, an `rgb()` call); a solid canvas
renders its declared matte into all four canvas corners while the source lands
intact at the centre, asserted against a wallpaper fixture of a DIFFERENT
colour so the test cannot pass by painting anything dark; and spelling
`{"kind": "wallpaper"}` out produces bytes identical to omitting it.

That last one has a stronger companion the suite cannot hold: re-running
`make demo-frame` on this repo must leave every wallpaper-canvas artifact
byte-identical to its committed version, with only the artifacts you meant to
move showing up as modified. `git status docs/assets/img/` after a render IS
that check — run it whenever `FrameStyle` gains a field, because a default that
shifts by one rounding step repaints the whole published tree without failing
one assertion.
