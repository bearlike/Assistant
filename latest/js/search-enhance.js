// Search UX polish for the shadcn theme:
//   1. Inject a magnifier icon + ⌘K hint into the navbar trigger so the
//      control reads as a search field at a glance.
//   2. Replace the theme's 300 ms debounced input handler with a snappier
//      60 ms one that also renders live "type more" / "searching…" / "no
//      results" hints into the results pane so the user always sees visible
//      feedback while typing. The theme's version rendered nothing at all
//      for short queries, which made search look broken.
//   3. Rewrite search result URLs when browsing an alias version (e.g.
//      /latest/) whose HTML has base_url hardcoded to the canonical version
//      (e.g. /main/). mike --alias-type copy copies the HTML verbatim, so
//      without this fix every search result on /latest/ links to /main/.
//
// Does not touch the search index, the worker, or any theme JS.

(function () {
  const MIN_QUERY = 3;
  const DEBOUNCE_MS = 60;
  const RESULTS_ID = "mkdocs-search-results";

  // --- version URL rewriter ------------------------------------------------
  // base_url is a top-level const injected by MkDocs into the page HTML:
  //   const base_url = "https://docs.mewbo.com/main";
  // It is always the canonical version's URL even when browsing an alias
  // (e.g. /latest/). Build a from→to pair once, then patch every result link.

  const versionRewriter = (() => {
    let from = null;
    let to = null;

    try {
      // base_url is declared in a sibling <script> tag and is accessible here
      // because top-level const/let in classic scripts share the global scope.
      /* global base_url */
      if (typeof base_url !== "undefined" && base_url) {
        const builtVersion = base_url.replace(/\/$/, "").split("/").pop(); // "main"
        const currentVersion = window.location.pathname.split("/").filter(Boolean)[0]; // "latest"
        if (builtVersion && currentVersion && builtVersion !== currentVersion) {
          from = base_url.replace(/\/$/, ""); // "https://docs.mewbo.com/main"
          to = `${window.location.origin}/${currentVersion}`; // "https://docs.mewbo.com/latest"
        }
      }
    } catch (_) {}

    return function rewrite(container) {
      if (!from || !to) return;
      container.querySelectorAll("a[href]").forEach((a) => {
        const href = a.getAttribute("href");
        if (href && href.startsWith(from)) {
          a.setAttribute("href", to + href.slice(from.length));
        }
      });
    };
  })();

  const attachResultsRewriter = () => {
    const el = document.getElementById(RESULTS_ID);
    if (!el || el.dataset.rewriter === "1") return;
    el.dataset.rewriter = "1";
    // Rewrite any links already present, then watch for new ones.
    versionRewriter(el);
    new MutationObserver(() => versionRewriter(el)).observe(el, {
      childList: true,
      subtree: true,
    });
  };

  // --- trigger decoration --------------------------------------------------

  const searchIconSVG = () => {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("xmlns", ns);
    svg.setAttribute("width", "16");
    svg.setAttribute("height", "16");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("class", "lucide lucide-search size-4 shrink-0 opacity-60");
    svg.setAttribute("aria-hidden", "true");
    const circle = document.createElementNS(ns, "circle");
    circle.setAttribute("cx", "11");
    circle.setAttribute("cy", "11");
    circle.setAttribute("r", "8");
    const line = document.createElementNS(ns, "path");
    line.setAttribute("d", "m21 21-4.3-4.3");
    svg.appendChild(circle);
    svg.appendChild(line);
    return svg;
  };

  const shortcutHint = () => {
    const kbd = document.createElement("kbd");
    kbd.className = "search-shortcut";
    kbd.setAttribute("aria-hidden", "true");
    const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.userAgent);
    kbd.textContent = isMac ? "\u2318K" : "Ctrl K";
    return kbd;
  };

  const enhanceTrigger = () => {
    const trigger = document.querySelector('button[onclick^="onSearchBarClick"]');
    if (!trigger || trigger.dataset.enhanced === "1") {
      return;
    }
    trigger.dataset.enhanced = "1";
    trigger.classList.add("search-trigger");
    trigger.insertBefore(searchIconSVG(), trigger.firstChild);
    trigger.appendChild(shortcutHint());
  };

  // --- live input feedback -------------------------------------------------

  const renderHint = (text, variant) => {
    const el = document.getElementById(RESULTS_ID);
    if (!el) return;
    el.innerHTML = "";
    const p = document.createElement("p");
    p.className = "search-hint" + (variant ? " " + variant : "");
    p.textContent = text;
    el.appendChild(p);
  };

  // Reach the mkdocs search worker. The theme's main.js declares it with a
  // bareword `var searchWorker = new Worker(...)` — in modern browsers that
  // exposes it on window, but we look via several paths so we don't care.
  const getWorker = () => {
    if (typeof window.searchWorker !== "undefined" && window.searchWorker) {
      return window.searchWorker;
    }
    try {
      const fn = new Function(
        "return typeof searchWorker !== 'undefined' ? searchWorker : null"
      );
      return fn();
    } catch (e) {
      return null;
    }
  };

  // Post to the worker with short retries, since some pages race between
  // the input handler getting attached and main.js instantiating the worker.
  const postWhenReady = (lunrQuery, attempt) => {
    attempt = attempt || 0;
    const worker = getWorker();
    if (worker) {
      worker.postMessage({ query: lunrQuery });
      return true;
    }
    if (attempt < 20) {
      setTimeout(() => postWhenReady(lunrQuery, attempt + 1), 250);
      return false;
    }
    renderHint("Search index still loading, try again in a second.", "loading");
    return false;
  };

  const attachInput = () => {
    const input = document.querySelector('input[cmdk-input]');
    if (!input || input.dataset.enhanced === "1") {
      return;
    }
    input.dataset.enhanced = "1";

    // Remove the theme's inline oninput so only our handler runs.
    if (input.hasAttribute("oninput")) {
      input.removeAttribute("oninput");
    }

    let debounceTimer = null;

    input.addEventListener("input", (event) => {
      const raw = (event.target.value || "").trim();

      if (debounceTimer) {
        clearTimeout(debounceTimer);
      }

      if (raw.length === 0) {
        renderHint("Start typing to search the docs.", "empty");
        return;
      }
      if (raw.length < MIN_QUERY) {
        renderHint(`Keep typing… (${MIN_QUERY - raw.length} more)`, "too-short");
        return;
      }

      renderHint("Searching…", "busy");

      debounceTimer = setTimeout(() => {
        // Lunr query: exact phrase boosted, prefix fallback, fuzzy fallback.
        const lunrQuery = `${raw}^10 ${raw}* ${raw}~1`;
        postWhenReady(lunrQuery);
      }, DEBOUNCE_MS);
    });

    // Prime the empty state so the user sees something the first time the
    // dialog opens with no query yet.
    renderHint("Start typing to search the docs.", "empty");
  };

  // --- bootstrap -----------------------------------------------------------

  const init = () => {
    enhanceTrigger();
    attachInput();
    attachResultsRewriter();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
