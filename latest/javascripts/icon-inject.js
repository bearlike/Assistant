const ADMONITION_ICONS = {
  note: "lucide:message-square",
  tip: "lucide:lightbulb",
  important: "lucide:circle-alert",
  warning: "lucide:triangle-alert",
  caution: "lucide:octagon-alert",
  danger: "lucide:octagon-alert",
};

const H2_ICONS = {
  target: "lucide:target",
  flow: "lucide:cpu",
  plug: "lucide:plug",
  grid: "lucide:layout-grid",
  route: "lucide:route",
  book: "lucide:book-open",
  star: "lucide:star",
};

function injectIcons() {
  document.querySelectorAll(".admonition-title").forEach((title) => {
    if (title.querySelector("iconify-icon")) return;
    const type = [...title.parentElement.classList].find(
      (c) => c !== "admonition"
    );
    const icon = ADMONITION_ICONS[type] || "lucide:info";
    title.insertAdjacentHTML(
      "afterbegin",
      `<iconify-icon icon="${icon}" width="16" height="16" aria-hidden="true"></iconify-icon>`
    );
  });

  document.querySelectorAll("h2.ms-h2-icon[data-icon]").forEach((h2) => {
    if (h2.querySelector("iconify-icon")) return;
    const icon = H2_ICONS[h2.dataset.icon] || "lucide:circle";
    h2.insertAdjacentHTML(
      "afterbegin",
      `<iconify-icon icon="${icon}" width="22" height="22" aria-hidden="true"></iconify-icon>`
    );
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", injectIcons);
} else {
  injectIcons();
}
