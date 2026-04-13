// Swap the Pygments stylesheet to match the active light/dark theme.
// The shadcn theme writes both stylesheets to the site but only links one when
// the legacy "codehilite" extension is present. We use pymdownx.highlight, so
// we inject the right <link> ourselves and re-point it when the theme flips.

(function () {
  const LIGHT = "css/pygments/shadcn-light.css";
  const DARK = "css/pygments/github-dark.css";
  const LINK_ID = "pygments-theme";

  // Reuse the page-relative prefix that MkDocs already wrote for base.css so we
  // don't have to recompute directory depth.
  function siteRoot() {
    const base = document.querySelector(
      "link[href$='/css/base.css'], link[href$='css/base.css']"
    );
    if (base) {
      return base.getAttribute("href").replace(/css\/base\.css$/, "");
    }
    return "";
  }

  function apply() {
    const isDark = document.documentElement.classList.contains("dark");
    const href = siteRoot() + (isDark ? DARK : LIGHT);
    let link = document.getElementById(LINK_ID);
    if (!link) {
      link = document.createElement("link");
      link.id = LINK_ID;
      link.rel = "stylesheet";
      document.head.appendChild(link);
    }
    if (link.getAttribute("href") !== href) {
      link.setAttribute("href", href);
    }
  }

  apply();

  if (window.MutationObserver) {
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.attributeName === "class") {
          apply();
          return;
        }
      }
    }).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
  }
})();
