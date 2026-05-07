/**
 * Cloudflare Worker for docs sites built with mkdocs-shadcn-mewbo.
 *
 * Sits in front of the docs origin (GitHub Pages, R2, S3, etc.) and:
 *   - Serves three /.well-known/ endpoints inline so AI agents can
 *     discover the product's API catalog, MCP server, and Agent Skills:
 *       * /.well-known/api-catalog              (RFC 9727 linkset)
 *       * /.well-known/mcp/server-card.json     (SEP-1649)
 *       * /.well-known/agent-skills/index.json  (Agent Skills v0.2.0)
 *   - Injects RFC 8288 `Link:` headers on every HTML response pointing
 *     agents at those endpoints + the human reference docs page.
 *
 * All product-specific values come from `env` (wrangler.toml [vars]).
 * The worker.js itself is product-neutral — copy it verbatim into your
 * repo and configure via vars.
 *
 * Required vars:
 *   SITE_URL                — absolute docs origin (e.g. "https://docs.mewbo.com")
 *   API_URL                 — absolute API origin (e.g. "https://api.mewbo.com")
 *
 * Optional vars:
 *   MCP_SERVER_NAME         — defaults to "Docs"
 *   MCP_SERVER_VERSION      — defaults to "1.0.0"
 *   MCP_SERVER_URL          — defaults to `${API_URL}/mcp`
 *   API_OPENAPI_URL         — defaults to `${API_URL}/swagger.json`
 *   API_HEALTH_URL          — defaults to `${API_URL}/healthz`
 *   REFERENCE_PATH          — defaults to "/reference/" (relative to SITE_URL)
 *   SKILLS_INDEX_JSON       — JSON-encoded Agent Skills index; defaults to
 *                             an empty list with the v0.2.0 schema.
 *
 * Deploy:
 *   cd <your-repo>/.github/cloudflare && wrangler deploy
 */

const REL_API_CATALOG = "api-catalog";
const REL_SERVICE_DOC = "service-doc";
const REL_MCP_SERVER_CARD =
  "https://modelcontextprotocol.io/ns/server-card";
const SKILLS_SCHEMA = "https://agentskills.io/schema/v0.2.0/index.json";

function buildWellKnown(env) {
  const SITE = env.SITE_URL;
  const API = env.API_URL;
  const MCP_NAME = env.MCP_SERVER_NAME ?? "Docs";
  const MCP_VERSION = env.MCP_SERVER_VERSION ?? "1.0.0";
  const MCP_URL = env.MCP_SERVER_URL ?? `${API}/mcp`;
  const OPENAPI = env.API_OPENAPI_URL ?? `${API}/swagger.json`;
  const HEALTH = env.API_HEALTH_URL ?? `${API}/healthz`;
  const REFPATH = env.REFERENCE_PATH ?? "/reference/";

  let skillsIndex;
  try {
    skillsIndex = env.SKILLS_INDEX_JSON
      ? JSON.parse(env.SKILLS_INDEX_JSON)
      : { $schema: SKILLS_SCHEMA, skills: [] };
  } catch (_) {
    skillsIndex = { $schema: SKILLS_SCHEMA, skills: [] };
  }

  return {
    "/.well-known/api-catalog": {
      type: "application/linkset+json",
      body: JSON.stringify({
        linkset: [
          {
            anchor: API + "/",
            "service-doc": [{ href: SITE + REFPATH }],
            "service-desc": [{ href: OPENAPI }],
            status: [{ href: HEALTH }],
          },
        ],
      }),
    },
    "/.well-known/mcp/server-card.json": {
      type: "application/json",
      body: JSON.stringify({
        serverInfo: { name: MCP_NAME, version: MCP_VERSION },
        transport: { type: "streamable-http", url: MCP_URL },
        capabilities: { tools: {}, resources: {}, prompts: {} },
      }),
    },
    "/.well-known/agent-skills/index.json": {
      type: "application/json",
      body: JSON.stringify(skillsIndex),
    },
  };
}

function buildLinkHeaders(env) {
  const REFPATH = env.REFERENCE_PATH ?? "/reference/";
  return [
    `</.well-known/api-catalog>; rel="${REL_API_CATALOG}"`,
    `<${REFPATH}>; rel="${REL_SERVICE_DOC}"`,
    `</.well-known/mcp/server-card.json>; rel="${REL_MCP_SERVER_CARD}"`,
  ];
}

export default {
  async fetch(request, env) {
    if (!env.SITE_URL || !env.API_URL) {
      return new Response(
        "mewbo-docs worker: SITE_URL and API_URL must be set in wrangler.toml [vars].",
        { status: 500 }
      );
    }

    const url = new URL(request.url);
    const wellKnown = buildWellKnown(env);
    const entry = wellKnown[url.pathname];

    if (entry) {
      return new Response(entry.body, {
        headers: {
          "Content-Type": entry.type,
          "Cache-Control": "public, max-age=3600",
          "Access-Control-Allow-Origin": "*",
        },
      });
    }

    // Pass-through to origin; append Link headers on HTML responses only.
    const response = await fetch(request);
    const headers = new Headers(response.headers);
    const ct = headers.get("Content-Type") || "";
    if (ct.includes("text/html")) {
      for (const link of buildLinkHeaders(env)) {
        headers.append("Link", link);
      }
    }

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  },
};
