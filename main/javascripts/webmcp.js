// WebMCP: expose docs search to AI agents visiting the page.
// Spec: https://webmachinelearning.github.io/webmcp/
// Only runs when the browser supports navigator.modelContext (e.g. Chrome with WebMCP flag).
if (typeof navigator !== "undefined" && navigator.modelContext) {
  const controller = new AbortController();

  navigator.modelContext.registerTool(
    {
      name: "search_mewbo_docs",
      description:
        "Full-text search across Mewbo documentation. Returns matching page titles, URLs, and excerpts.",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Search terms" },
          limit: { type: "number", description: "Max results (default 5)" },
        },
        required: ["query"],
      },
      execute: async ({ query, limit = 5 }) => {
        // Derive the versioned base from the page's <base> tag (injected by MkDocs)
        const base = document.querySelector("base")?.href ?? location.origin + "/latest/";
        const idx  = await fetch(base + "search/search_index.json").then((r) => r.json());
        const q    = query.toLowerCase();
        const hits = idx.docs
          .filter((d) => d.title.toLowerCase().includes(q) || d.text.toLowerCase().includes(q))
          .slice(0, limit)
          .map((d) => ({
            title:   d.title,
            url:     new URL(d.location, base).href,
            excerpt: d.text.slice(0, 300),
          }));
        return { results: hits, total: hits.length };
      },
    },
    { signal: controller.signal }
  );

  // Clean up on page unload so the browser doesn't accumulate stale tools
  window.addEventListener("unload", () => controller.abort(), { once: true });
}
