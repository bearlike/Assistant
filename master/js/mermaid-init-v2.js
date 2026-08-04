/*
 * Mermaid rendering for the shadcn theme.
 *
 * Three jobs, in order:
 *   1. Configure mermaid from the LIVE theme tokens so a diagram is drawn in
 *      the page's own palette and typeface rather than mermaid's stock one.
 *   2. Render every `pre.mermaid` fence into a framed, click-to-expand card.
 *   3. Provide the expanded view: a native <dialog> stage with zoom, pan and
 *      fit-to-stage, matching the reading affordances the Mewbo console gives
 *      the same diagrams.
 *
 * The fence markup comes from pymdownx.superfences `custom_fences` with
 * `class: mermaid`, i.e. `<pre class="mermaid"><code>…</code></pre>`. Without
 * that extension configured, a ```mermaid block is highlighted as plain text
 * and nothing here matches — which is silent, so the demo site carries a
 * fixture page to keep it exercised.
 */

/* ── Theme tokens ─────────────────────────────────────────────────────────
 * Mermaid does not consume CSS variables. Its layout engine measures text on
 * a Canvas 2D context and it hands colours to an internal colour library, so
 * both have to arrive as already-resolved literal values.
 *
 * Colours are resolved THROUGH A CANVAS rather than passed along verbatim.
 * The theme's tokens are authored in `oklch()`, which mermaid's colour library
 * does not parse; painting the computed value onto a 1x1 canvas and reading
 * the pixel back delegates parsing to the browser and yields a plain hex
 * string that every mermaid version understands. That also means this keeps
 * working if the tokens are ever re-authored in another colour space.
 */

const COLOR_PROBE = (() => {
  let ctx = null;
  return () => {
    if (ctx) return ctx;
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    ctx = canvas.getContext("2d", { willReadFrequently: true });
    return ctx;
  };
})();

const toHex = (value, fallback) => {
  const ctx = COLOR_PROBE();
  if (!ctx || !value) return fallback;
  try {
    // Painting over an opaque base keeps a translucent token (the theme's
    // hairline borders carry alpha) from reading back as near-transparent
    // black once mermaid bakes it into the SVG as a flat stroke.
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = "#000000";
    ctx.fillStyle = value;
    // A value the browser cannot parse leaves fillStyle at the previous
    // assignment, so an unparseable token falls back rather than painting black.
    if (ctx.fillStyle === "#000000" && value.trim() !== "#000000") {
      const probe = ctx.fillStyle;
      if (probe === "#000000") return fallback;
    }
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    return (
      "#" +
      [r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("")
    );
  } catch {
    return fallback;
  }
};

const tokenColor = (name, fallback) => {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return toHex(raw, fallback);
};

/**
 * Mermaid feeds this string straight into `ctx.font` to measure label text
 * while it lays the diagram out, so it must be a resolved stack and not a
 * `var(--font-sans)` reference — that resolves in a stylesheet but not on a
 * canvas, and the silent result is every node sized for the wrong typeface.
 */
const tokenFont = () => {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue("--font-sans")
    .trim();
  return raw || "system-ui, sans-serif";
};

const isDarkMode = () => document.documentElement.classList.contains("dark");

const mermaidConfig = () => {
  const dark = isDarkMode();
  const fontFamily = tokenFont();
  return {
    startOnLoad: false,
    securityLevel: "loose",
    fontFamily,
    // `base` is the ONLY theme mermaid lets themeVariables modify. Under
    // `default` or `dark` it computes its own palette and silently discards
    // the overrides — which renders a diagram in mermaid's stock lavender no
    // matter how carefully the tokens below are resolved. `darkMode` is what
    // tells `base` which direction to derive its remaining shades in.
    theme: "base",
    themeVariables: {
      darkMode: dark,
      // Every colour below comes from the `--diagram-*` family, NOT from the
      // chrome tokens. Chrome is tuned to recede; a diagram has to be read.
      // Pointing `primaryColor` at `--card` is what produced a node filled
      // with the exact colour of the card behind it. See the "Diagram tokens"
      // block in mewbo.css for the measured ratios.
      background: tokenColor("--diagram-surface", dark ? "#121110" : "#f2f0ea"),
      primaryColor: tokenColor("--diagram-node-bg", dark ? "#26241f" : "#ffffff"),
      primaryTextColor: tokenColor("--diagram-label", dark ? "#f8f8f6" : "#0a0a0a"),
      primaryBorderColor: tokenColor("--diagram-node-border", dark ? "#767269" : "#8c8880"),
      lineColor: tokenColor("--diagram-line", dark ? "#8d8a80" : "#6f6c64"),
      // Left unstated, `base` derives these by rotating the primary hue, so
      // subgraphs and alternating rows come out in colours that appear
      // nowhere else on the site. Point them at real surface tokens instead.
      secondaryColor: tokenColor("--diagram-cluster", dark ? "#1c1b18" : "#e7e4db"),
      tertiaryColor: tokenColor("--diagram-surface", dark ? "#121110" : "#f2f0ea"),
      secondaryBorderColor: tokenColor("--diagram-node-border", dark ? "#767269" : "#8c8880"),
      tertiaryBorderColor: tokenColor("--diagram-node-border", dark ? "#767269" : "#8c8880"),
      secondaryTextColor: tokenColor("--diagram-label", dark ? "#f8f8f6" : "#0a0a0a"),
      tertiaryTextColor: tokenColor("--diagram-label", dark ? "#f8f8f6" : "#0a0a0a"),
      // Mermaid derives the edge-label chip fill from its OWN palette rather
      // than from `background`, so leaving this unset renders a flowchart
      // branch's yes/no label as grey-on-grey against a dark diagram.
      edgeLabelBackground: tokenColor("--diagram-surface", dark ? "#121110" : "#f2f0ea"),
      textColor: tokenColor("--diagram-label", dark ? "#f8f8f6" : "#0a0a0a"),
      nodeTextColor: tokenColor("--diagram-label", dark ? "#f8f8f6" : "#0a0a0a"),
      mainBkg: tokenColor("--diagram-node-bg", dark ? "#26241f" : "#ffffff"),
      nodeBorder: tokenColor("--diagram-node-border", dark ? "#767269" : "#8c8880"),
      clusterBkg: tokenColor("--diagram-cluster", dark ? "#1c1b18" : "#e7e4db"),
      clusterBorder: tokenColor("--diagram-node-border", dark ? "#767269" : "#8c8880"),
      titleColor: tokenColor("--diagram-label", dark ? "#f8f8f6" : "#0a0a0a"),
      fontFamily,
    },
    flowchart: {
      // HTML labels render as foreignObject, which is what makes wrapped and
      // multi-line label text stylable from CSS (`.nodeLabel`) at all. With
      // htmlLabels off, labels are <text>/<tspan> and no amount of CSS fixes
      // their line spacing.
      htmlLabels: true,
      // Wider labels wrap onto fewer lines, so a node grows sideways instead
      // of downwards. This is the honest lever for the "prefer horizontal
      // growth" goal: mermaid has no theme-side control over rank direction
      // (TD vs LR lives in the author's source), but it does control how much
      // text sits on one line before wrapping.
      wrappingWidth: 260,
      nodeSpacing: 45,
      rankSpacing: 55,
      padding: 12,
      curve: "basis",
      useMaxWidth: true,
    },
    sequence: { useMaxWidth: true, wrap: true },
    class: { useMaxWidth: true },
    gantt: { useMaxWidth: true },
  };
};

/* ── Render cache ─────────────────────────────────────────────────────────
 * Keyed `theme|source`. A theme flip and back is free, and opening the
 * expanded view never re-invokes mermaid for a diagram already on the page.
 */
const svgCache = new Map();

let initialisedTheme = null;

const ensureInitialised = () => {
  const theme = isDarkMode() ? "dark" : "light";
  if (initialisedTheme !== theme) {
    window.mermaid.initialize(mermaidConfig());
    initialisedTheme = theme;
  }
  return theme;
};

let renderSeq = 0;

const renderToSvg = async (source) => {
  const theme = ensureInitialised();
  const key = `${theme}|${source}`;
  const hit = svgCache.get(key);
  if (hit) return hit;
  const { svg } = await window.mermaid.render(
    `mmd-${Date.now()}-${renderSeq++}`,
    source
  );
  svgCache.set(key, svg);
  return svg;
};

/* ── Inline card ──────────────────────────────────────────────────────────
 * A diagram is a figure with an expand control, not a bare SVG: it gets the
 * same framed treatment as content images so it reads as a deliberate object
 * on the page. The whole card is the click target; the button is the visible
 * affordance and the keyboard path.
 */

const EXPAND_ICON =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>';

/**
 * Fitting the diagram to the column is the DEFAULT, and the bar for giving up
 * on it is deliberately low.
 *
 * The inline card is a preview — Expand is one click away and renders the
 * diagram at full size with zoom and pan — so a preview that is small but
 * whole beats one that is cropped behind a scrollbar. An earlier, stricter
 * value sent most real diagrams to the scroll path, which reads as the card
 * failing to lay the diagram out rather than as a deliberate affordance.
 *
 * So only a diagram close to three times the column width falls back to
 * scrolling at natural size; everything narrower shrinks to fit.
 */
const LEGIBLE_SCALE = 0.35;

/**
 * The diagram's natural size in CSS pixels, independent of any transform.
 *
 * `useMaxWidth` makes mermaid write the natural width into `max-width`, and
 * the viewBox carries the aspect ratio, so the size is knowable without
 * measuring a painted box. That matters because the painted box sits inside a
 * transformed, TRANSITIONED wrapper: measuring it means either dividing by a
 * scale that may not be the one on screen, or switching the transform off and
 * reading back before the transition has applied. Both were tried; both were
 * wrong. One derivation, used by the inline card and by the viewer.
 */
const naturalSize = (svg) => {
  const box = svg.viewBox && svg.viewBox.baseVal;
  const declared = parseFloat(svg.style.maxWidth || "");
  if (box && box.width > 0 && box.height > 0) {
    const w = declared || box.width;
    return { w, h: box.height * (w / box.width) };
  }
  const rect = svg.getBoundingClientRect();
  return { w: declared || rect.width, h: rect.height };
};

/**
 * Tag a figure as too wide or too tall to show whole, so CSS can switch it
 * from "shrink to fit" to "scroll at a readable size" and reveal the expand
 * affordance permanently rather than only on hover.
 */
const classifyOverflow = (figure) => {
  const stage = figure.querySelector(".ms-mermaid__stage");
  const svg = stage && stage.querySelector("svg");
  if (!stage || !svg) return;

  const available = stage.clientWidth;
  const naturalW = naturalSize(svg).w || available;
  if (available > 0 && naturalW / available > 1 / LEGIBLE_SCALE) {
    figure.dataset.wide = "1";
    // `width: auto` cannot recover the natural size: mermaid ships the SVG
    // with a `width="100%"` attribute, which leaves it no intrinsic width to
    // fall back to, so it keeps filling the column. Pin the measured width
    // instead — CSS alone genuinely cannot express this one.
    svg.style.width = `${naturalW}px`;
  }

  if (stage.scrollHeight > stage.clientHeight + 1) {
    figure.dataset.tall = "1";
  }
};

const buildCard = (source, svg, index) => {
  const figure = document.createElement("figure");
  figure.className = "ms-mermaid";
  figure.dataset.source = source;
  figure.dataset.diagramId = `diagram-${index + 1}`;

  const stage = document.createElement("div");
  stage.className = "ms-mermaid__stage";
  stage.innerHTML = svg;

  const expand = document.createElement("button");
  expand.type = "button";
  expand.className = "ms-mermaid__expand";
  expand.innerHTML = `${EXPAND_ICON}<span>Expand</span>`;
  expand.setAttribute("aria-label", "Expand diagram to full view");
  expand.title = "Expand diagram";

  figure.append(stage, expand);

  // Fit to the column first; fall back to scrolling only when the diagram is
  // so much wider than the column that shrinking it would leave nothing
  // readable at all. See LEGIBLE_SCALE.
  //
  // Measured once after layout: `useMaxWidth` gives the SVG a max-width in
  // px equal to its natural width, so the ratio is available without
  // re-rendering anything.
  requestAnimationFrame(() => classifyOverflow(figure));

  const open = () => openViewer(figure);
  expand.addEventListener("click", (e) => {
    e.stopPropagation();
    open();
  });
  figure.addEventListener("click", open);

  return figure;
};

const buildError = (source, message) => {
  const figure = document.createElement("figure");
  figure.className = "ms-mermaid ms-mermaid--error";
  figure.dataset.source = source;
  const note = document.createElement("p");
  note.className = "ms-mermaid__error";
  note.textContent = message;
  const pre = document.createElement("pre");
  pre.className = "ms-mermaid__source";
  pre.textContent = source;
  figure.append(note, pre);
  return figure;
};

const renderMermaid = () => {
  if (!window.mermaid) return;
  const blocks = Array.from(document.querySelectorAll("pre.mermaid code"));
  blocks.forEach((code, idx) => {
    const pre = code.parentElement;
    if (!pre) return;
    const source = code.textContent || "";
    // Reserve the slot synchronously so a slow render cannot leave a raw,
    // pygments-highlighted fence visible while mermaid loads.
    const placeholder = document.createElement("figure");
    placeholder.className = "ms-mermaid ms-mermaid--pending";
    placeholder.dataset.source = source;
    placeholder.innerHTML =
      '<p class="ms-mermaid__pending">Rendering diagram…</p>';
    pre.replaceWith(placeholder);

    renderToSvg(source)
      .then((svg) => placeholder.replaceWith(buildCard(source, svg, idx)))
      .catch(() =>
        placeholder.replaceWith(
          buildError(source, "This diagram could not be rendered.")
        )
      );
  });
};

/* ── Expanded viewer ──────────────────────────────────────────────────────
 * One <dialog> for the whole page, reused per diagram — the same native
 * dialog + ::backdrop mechanism the search modal uses, so Escape, focus
 * trapping and the backdrop come from the platform rather than hand-rolled.
 */

const MIN_SCALE = 0.2;
const MAX_SCALE = 6;
const FIT_MARGIN = 48;

const viewer = {
  dialog: null,
  stage: null,
  inner: null,
  label: null,
  scale: 1,
  fitScale: 1,
  pos: { x: 0, y: 0 },
  drag: null,
};

const applyTransform = () => {
  if (!viewer.inner) return;
  viewer.inner.style.transform =
    `translate(calc(-50% + ${viewer.pos.x}px), calc(-50% + ${viewer.pos.y}px))` +
    ` scale(${viewer.scale})`;
};

const setScale = (next) => {
  viewer.scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, next));
  applyTransform();
};

/**
 * Scale the diagram so it fills the stage with a comfortable margin.
 *
 * The natural size is measured with the transform switched OFF, rather than
 * by dividing the painted rect by the current scale. That division looked
 * equivalent and was not: it assumes the transform on screen matches
 * `viewer.scale`, and on opening a diagram the wrapper still carries the
 * PREVIOUS diagram's scale, so the measurement came back inflated by it and
 * the fit came out as `correct / previous`. Opening two diagrams alternately
 * compounded that every time — 3.50, then 0.49, then clamped at 6.
 *
 * Since this code owns the transform, the honest measurement is simply to
 * take it off first. No arithmetic, and nothing to keep in sync.
 */
const recomputeFit = () => {
  if (!viewer.stage || !viewer.inner) return;
  const svg = viewer.inner.querySelector("svg");
  if (!svg) return;
  const stageRect = viewer.stage.getBoundingClientRect();
  const { w: naturalW, h: naturalH } = naturalSize(svg);
  if (naturalW <= 0 || naturalH <= 0) return;
  const fit = Math.min(
    (stageRect.width - FIT_MARGIN) / naturalW,
    (stageRect.height - FIT_MARGIN) / naturalH
  );
  viewer.fitScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, fit));
  viewer.pos = { x: 0, y: 0 };
  setScale(viewer.fitScale);
};

const buildViewer = () => {
  if (viewer.dialog) return viewer.dialog;

  const dialog = document.createElement("dialog");
  dialog.className = "ms-diagram-viewer";
  dialog.innerHTML = `
    <div class="ms-diagram-viewer__bar">
      <span class="ms-diagram-viewer__label">${EXPAND_ICON}<span data-role="label"></span></span>
      <div class="ms-diagram-viewer__actions">
        <button type="button" data-act="out" aria-label="Zoom out" title="Zoom out">&minus;</button>
        <button type="button" data-act="reset" aria-label="Reset zoom" title="Reset zoom">&#8635;</button>
        <button type="button" data-act="in" aria-label="Zoom in" title="Zoom in">&plus;</button>
        <button type="button" data-act="close" aria-label="Close diagram" title="Close">&times;</button>
      </div>
    </div>
    <div class="ms-diagram-viewer__stage">
      <div class="ms-diagram-viewer__inner"></div>
    </div>
    <p class="ms-diagram-viewer__hint">Scroll to zoom · drag to pan · Esc to close</p>
  `;
  document.body.appendChild(dialog);

  viewer.dialog = dialog;
  viewer.stage = dialog.querySelector(".ms-diagram-viewer__stage");
  viewer.inner = dialog.querySelector(".ms-diagram-viewer__inner");
  viewer.label = dialog.querySelector("[data-role=label]");

  dialog.querySelector("[data-act=close]").addEventListener("click", () =>
    dialog.close()
  );
  dialog
    .querySelector("[data-act=in]")
    .addEventListener("click", () => setScale(viewer.scale * 1.1));
  dialog
    .querySelector("[data-act=out]")
    .addEventListener("click", () => setScale(viewer.scale * 0.9));
  dialog.querySelector("[data-act=reset]").addEventListener("click", () => {
    viewer.pos = { x: 0, y: 0 };
    setScale(viewer.fitScale);
  });

  viewer.stage.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      setScale(viewer.scale * (e.deltaY > 0 ? 0.92 : 1.08));
    },
    { passive: false }
  );

  viewer.stage.addEventListener("pointerdown", (e) => {
    viewer.drag = {
      x: e.clientX,
      y: e.clientY,
      sx: viewer.pos.x,
      sy: viewer.pos.y,
    };
    viewer.inner.style.transition = "none";
    viewer.stage.setPointerCapture(e.pointerId);
    viewer.stage.dataset.dragging = "1";
  });
  viewer.stage.addEventListener("pointermove", (e) => {
    if (!viewer.drag) return;
    viewer.pos = {
      x: viewer.drag.sx + (e.clientX - viewer.drag.x),
      y: viewer.drag.sy + (e.clientY - viewer.drag.y),
    };
    applyTransform();
  });
  const endDrag = () => {
    viewer.drag = null;
    viewer.inner.style.transition = "";
    delete viewer.stage.dataset.dragging;
  };
  viewer.stage.addEventListener("pointerup", endDrag);
  viewer.stage.addEventListener("pointercancel", endDrag);

  // Clicking the backdrop closes. The stage fills the dialog, so a click that
  // lands on the dialog element itself is necessarily outside the content.
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) dialog.close();
  });

  window.addEventListener("resize", () => {
    if (dialog.open) recomputeFit();
  });

  return dialog;
};

const openViewer = (figure) => {
  const source = figure.dataset.source || "";
  if (!source) return;
  const dialog = buildViewer();
  viewer.label.textContent = figure.dataset.diagramId || "Diagram";
  viewer.pos = { x: 0, y: 0 };
  viewer.scale = 1;
  viewer.inner.innerHTML = "";

  const paint = (svg) => {
    viewer.inner.innerHTML = svg;
    // Pin the SVG to its natural width. Mermaid ships `width="100%"`, and the
    // viewer clears `max-width`, so inside the shrink-to-fit wrapper the
    // percentage has nothing to resolve against and collapses to a default a
    // few hundred pixels wide. Painted size then disagrees with natural size
    // and the fit is computed against a diagram that is not the one on
    // screen. The inline card pins the width for the same reason.
    const el = viewer.inner.querySelector("svg");
    if (el) el.style.width = `${naturalSize(el).w}px`;
    // Fit after the browser has laid the SVG out, otherwise the measured
    // rect is zero and the diagram opens at an arbitrary scale.
    requestAnimationFrame(recomputeFit);
  };

  const cached = svgCache.get(`${isDarkMode() ? "dark" : "light"}|${source}`);
  if (cached) paint(cached);

  if (!dialog.open) dialog.showModal();

  if (!cached) {
    renderToSvg(source)
      .then(paint)
      .catch(() => {
        viewer.inner.textContent = "This diagram could not be rendered.";
      });
  }
};

/* ── Theme flips ──────────────────────────────────────────────────────────
 * Re-render every card against the incoming palette. Cache hits make a flip
 * back to a theme already seen instant.
 */
const rerenderAll = () => {
  if (!window.mermaid) return;
  ensureInitialised();
  const figures = Array.from(
    document.querySelectorAll("figure.ms-mermaid[data-source]")
  );
  figures.forEach((figure) => {
    const source = figure.dataset.source || "";
    const stage = figure.querySelector(".ms-mermaid__stage");
    if (!source || !stage) return;
    renderToSvg(source)
      .then((svg) => {
        stage.innerHTML = svg;
      })
      .catch(() => {
        /* keep the diagram that is already on screen */
      });
  });
  if (viewer.dialog && viewer.dialog.open) {
    const open = document.querySelector("figure.ms-mermaid[data-source]");
    if (open) {
      renderToSvg(viewer.dialog.dataset.source || open.dataset.source || "")
        .then((svg) => {
          viewer.inner.innerHTML = svg;
          requestAnimationFrame(recomputeFit);
        })
        .catch(() => {});
    }
  }
};

const observeThemeChanges = () => {
  if (!window.MutationObserver) return;
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.attributeName === "class") {
        rerenderAll();
        return;
      }
    }
  });
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });
};

const boot = () => {
  renderMermaid();
  observeThemeChanges();
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
