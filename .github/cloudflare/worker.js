/**
 * Cloudflare Worker for docs.mewbo.com
 *
 * Deploy: from repo root, `cd .github/cloudflare && npx wrangler deploy`
 *         (route docs.mewbo.com/* is configured in wrangler.toml; the
 *         CNAME for docs must be orange-cloud proxied for it to fire)
 *
 * Handles:
 *   - /.well-known/api-catalog               (RFC 9727, application/linkset+json)
 *   - /.well-known/mcp/server-card.json      (SEP-1649)
 *   - /.well-known/agent-skills/index.json   (Agent Skills Discovery v0.2.0)
 *   - All other requests                     (pass-through + RFC 8288 Link headers)
 */

const SITE = "https://docs.mewbo.com";
const API  = "https://api.mewbo.com";       // TODO: update if API base differs

// RFC 8288 Link headers injected on every HTML response
const LINK_HEADERS = [
  `</.well-known/api-catalog>; rel="api-catalog"`,
  `</reference/>; rel="service-doc"`,
  `</.well-known/mcp/server-card.json>; rel="https://modelcontextprotocol.io/ns/server-card"`,
];

// Static well-known responses (inline — avoids GitHub Pages MIME-type issues)
const WELL_KNOWN = {
  "/.well-known/api-catalog": {
    type: "application/linkset+json",
    body: JSON.stringify({
      linkset: [
        {
          anchor: API + "/",
          "service-doc":  [{ href: SITE + "/reference/" }],
          "service-desc": [{ href: API  + "/swagger.json" }],   // TODO: adjust OpenAPI spec URL
          status:         [{ href: API  + "/healthz" }],         // TODO: adjust health endpoint
        },
      ],
    }),
  },

  "/.well-known/mcp/server-card.json": {
    type: "application/json",
    // TODO: fill in once Mewbo exposes a public MCP server endpoint.
    // Remove this entry entirely if Mewbo is MCP-client-only.
    body: JSON.stringify({
      serverInfo: { name: "Mewbo", version: "1.0.0" },
      transport:  { type: "streamable-http", url: API + "/mcp" },
      capabilities: { tools: {}, resources: {}, prompts: {} },
    }),
  },

  "/.well-known/agent-skills/index.json": {
    type: "application/json",
    // TODO: add entries as Mewbo publishes agent skills
    body: JSON.stringify({
      $schema: "https://agentskills.io/schema/v0.2.0/index.json",
      skills: [],
    }),
  },
};

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const entry = WELL_KNOWN[url.pathname];

    // Serve well-known files directly with correct MIME types
    if (entry) {
      return new Response(entry.body, {
        headers: {
          "Content-Type":              entry.type,
          "Cache-Control":             "public, max-age=3600",
          "Access-Control-Allow-Origin": "*",
        },
      });
    }

    // Pass through to GitHub Pages origin, appending Link headers
    const response = await fetch(request);
    const headers  = new Headers(response.headers);

    // Only inject Link headers on HTML responses (skip assets)
    const ct = headers.get("Content-Type") || "";
    if (ct.includes("text/html")) {
      for (const link of LINK_HEADERS) {
        headers.append("Link", link);
      }
    }

    return new Response(response.body, {
      status:     response.status,
      statusText: response.statusText,
      headers,
    });
  },
};
