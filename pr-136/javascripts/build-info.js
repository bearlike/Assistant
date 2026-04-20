(() => {
  const CANDIDATES = [
    "build-info.json",
    "../build-info.json",
    "../../build-info.json",
    "../../../build-info.json",
    "../../../../build-info.json",
  ];

  const findFooter = () => {
    return (
      document.querySelector("footer") ||
      document.querySelector(".md-footer") ||
      document.querySelector(".footer")
    );
  };

  const findTarget = (footer) => {
    if (!footer) {
      return null;
    }
    return (
      footer.querySelector(".md-footer-meta__inner") ||
      footer.querySelector(".md-footer__inner") ||
      footer.querySelector("p") ||
      footer
    );
  };

  const fetchBuildInfo = async () => {
    for (const path of CANDIDATES) {
      try {
        const response = await fetch(path, { cache: "no-store" });
        if (!response.ok) {
          continue;
        }
        return await response.json();
      } catch (err) {
        continue;
      }
    }
    return null;
  };

  const inject = async () => {
    const footer = findFooter();
    const target = findTarget(footer);
    if (!target) {
      return;
    }
    if (target.querySelector(".build-info")) {
      return;
    }
    const info = await fetchBuildInfo();
    if (!info || !info.commit) {
      return;
    }
    const span = document.createElement("span");
    span.className = "build-info";
    span.textContent = ` · build ${info.commit}`;
    span.style.marginLeft = "0.25rem";
    span.style.opacity = "0.6";
    span.style.fontSize = "0.85em";
    target.appendChild(span);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      void inject();
    });
  } else {
    void inject();
  }
})();
