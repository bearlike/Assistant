const renderMermaid = () => {
  if (!window.mermaid) {
    return;
  }
  mermaid.initialize({ startOnLoad: false });
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
        container.innerHTML = svg;
        pre.replaceWith(container);
      })
      .catch(() => {
        const container = document.createElement("div");
        container.className = "mermaid";
        container.textContent = text;
        pre.replaceWith(container);
      });
  });
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", renderMermaid);
} else {
  renderMermaid();
}
