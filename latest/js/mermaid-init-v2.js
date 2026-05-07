const isDarkMode = () =>
  document.documentElement.classList.contains("dark");

const mermaidConfig = () => ({
  startOnLoad: false,
  theme: isDarkMode() ? "dark" : "default",
  securityLevel: "loose",
});

const renderMermaid = () => {
  if (!window.mermaid) {
    return;
  }
  mermaid.initialize(mermaidConfig());
  const blocks = Array.from(document.querySelectorAll("pre.mermaid code"));
  blocks.forEach((code, idx) => {
    const pre = code.parentElement;
    if (!pre) {
      return;
    }
    const text = code.textContent || "";
    const id = `mermaid-${Date.now()}-${idx}`;
    mermaid
      .render(id, text)
      .then(({ svg }) => {
        const container = document.createElement("div");
        container.className = "mermaid";
        container.dataset.source = text;
        container.innerHTML = svg;
        pre.replaceWith(container);
      })
      .catch(() => {
        const container = document.createElement("div");
        container.className = "mermaid";
        container.dataset.source = text;
        container.textContent = text;
        pre.replaceWith(container);
      });
  });
};

// Re-render previously rendered diagrams when the theme class on <html> flips.
const rerenderAll = () => {
  if (!window.mermaid) {
    return;
  }
  mermaid.initialize(mermaidConfig());
  const containers = Array.from(
    document.querySelectorAll("div.mermaid[data-source]")
  );
  containers.forEach((container, idx) => {
    const text = container.dataset.source || "";
    if (!text) {
      return;
    }
    const id = `mermaid-${Date.now()}-re-${idx}`;
    mermaid
      .render(id, text)
      .then(({ svg }) => {
        container.innerHTML = svg;
      })
      .catch(() => {
        container.textContent = text;
      });
  });
};

const observeThemeChanges = () => {
  if (!window.MutationObserver) {
    return;
  }
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

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    renderMermaid();
    observeThemeChanges();
  });
} else {
  renderMermaid();
  observeThemeChanges();
}
