/**
 * Tests for SettingsModel — the pure faceting/diff core of the Settings UI.
 *
 * The fixture mirrors the real Pydantic 2.12 shape:
 *   - top-level `properties.<section>` is a bare `$ref` (possibly with a
 *     sibling `description`) for submodel sections, OR an inline object with
 *     no `$ref`/`x-group` for fallback sections.
 *   - facet metadata (`x-group`/`x-order`/`x-advanced`) lives on the
 *     referenced `$defs.<Class>`.
 *   - field flags (`x-advanced`/`x-secret`/`writeOnly`) live on each scalar
 *     property inside `$defs.<Class>.properties.<field>`.
 */
import { describe, expect, it } from "vitest";

import { SettingsModel } from "./SettingsModel";
import { FACETS } from "./facets";

// --- fixture schema --------------------------------------------------------

const schema: Record<string, unknown> = {
  type: "object",
  title: "AppConfig",
  properties: {
    // models facet, order 1 — has a secret field + an advanced field.
    llm: { $ref: "#/$defs/LLMConfig", description: "Primary model settings." },
    // models facet, order 2 — sorts after llm within the same facet.
    embeddings: { $ref: "#/$defs/EmbeddingsConfig" },
    // agent facet, order 1.
    agent: { $ref: "#/$defs/AgentConfig" },
    // inline section, no $ref and no x-group → falls into "other".
    channels: {
      type: "object",
      title: "Channels",
      description: "Inbound channel adapters.",
      properties: {
        nextcloud_url: { type: "string", title: "Nextcloud URL" },
      },
    },
  },
  $defs: {
    LLMConfig: {
      type: "object",
      title: "LLM",
      "x-group": "models",
      "x-order": 1,
      properties: {
        model: {
          type: "string",
          title: "Model Name",
          description: "Which chat model to route to.",
        },
        api_key: {
          type: "string",
          title: "API Key",
          writeOnly: true,
          "x-secret": true,
        },
        proxy_model_prefix: {
          type: "string",
          title: "Proxy Model Prefix",
          "x-advanced": true,
        },
      },
    },
    EmbeddingsConfig: {
      type: "object",
      title: "Embeddings",
      "x-group": "models",
      "x-order": 2,
      properties: {
        embed_model: { type: "string", title: "Embedding Model" },
        // No title → exercises the prettify acronym fallback.
        api_base_url: { type: "string" },
        // No title, trailing "config" word → stripped by prettify.
        retry_config: { type: "string" },
        // Deprecated → must be dropped from fields entirely.
        legacy_flag: { type: "boolean", title: "Legacy Flag", deprecated: true },
      },
    },
    AgentConfig: {
      type: "object",
      title: "Agent",
      "x-group": "agent",
      "x-order": 1,
      "x-advanced": true,
      properties: {
        edit_tool: { type: "string", title: "Edit Tool" },
        lsp: { $ref: "#/$defs/LspConfig" },
      },
    },
    LspConfig: {
      type: "object",
      title: "LSP",
      properties: { enabled: { type: "boolean", title: "Enabled" } },
    },
  },
};

const config: Record<string, unknown> = {
  llm: { model: "gpt-5", api_key: "", proxy_model_prefix: "openai" },
  embeddings: { embed_model: "text-embedding-3-small" },
  agent: { edit_tool: "", lsp: { enabled: true } },
  channels: { nextcloud_url: "https://nc.example" },
};

const model = () => new SettingsModel(schema, config);

/** Fetch a section, failing the test (not a null deref) when it's absent. */
function sectionOf(id: string) {
  const section = model().section(id);
  if (!section) throw new Error(`section ${id} not found`);
  return section;
}

// --- grouping --------------------------------------------------------------

describe("SettingsModel.groups", () => {
  it("buckets sections into the right facet, ordered by x-order", () => {
    const groups = model().groups();
    const models = groups.find((g) => g.id === "models");
    expect(models?.sections.map((s) => s.id)).toEqual(["llm", "embeddings"]);

    const agent = groups.find((g) => g.id === "agent");
    expect(agent?.sections.map((s) => s.id)).toEqual(["agent"]);
  });

  it("routes an unannotated inline section to the 'other' facet", () => {
    const other = model()
      .groups()
      .find((g) => g.id === "other");
    expect(other?.sections.map((s) => s.id)).toEqual(["channels"]);
  });

  it("returns facets in their declared presentation order", () => {
    const ids = model()
      .groups()
      .map((g) => g.id);
    expect(ids).toEqual(FACETS.map((f) => f.id));
  });

  it("includes empty facets (e.g. security) so the shell can render them", () => {
    const security = model()
      .groups()
      .find((g) => g.id === "security");
    expect(security).toBeDefined();
    expect(security?.sections).toEqual([]);
    expect(security?.iconName).toBe("Shield");
  });

  it("carries section-level x-advanced and resolves titles from the def", () => {
    const agent = model().section("agent");
    expect(agent?.advanced).toBe(true);
    expect(agent?.title).toBe("Agent");
    // section description comes from the $ref-sibling, falling back to def.
    expect(model().section("llm")?.description).toBe("Primary model settings.");
  });
});

// --- field flags -----------------------------------------------------------

describe("SettingsModel field flags", () => {
  it("derives secret from x-secret OR writeOnly", () => {
    const apiKey = sectionOf("llm").fields.find((f) => f.key === "api_key");
    expect(apiKey?.secret).toBe(true);

    const modelField = sectionOf("llm").fields.find((f) => f.key === "model");
    expect(modelField?.secret).toBe(false);
  });

  it("reads x-advanced on individual fields", () => {
    const prefix = sectionOf("llm").fields.find(
      (f) => f.key === "proxy_model_prefix"
    );
    expect(prefix?.advanced).toBe(true);
  });

  it("prettifies a key when the field has no title", () => {
    const inline = sectionOf("channels").fields.find(
      (f) => f.key === "nextcloud_url"
    );
    expect(inline?.title).toBe("Nextcloud URL");
  });

  it("uppercases known acronyms in the prettify fallback", () => {
    const field = sectionOf("embeddings").fields.find(
      (f) => f.key === "api_base_url"
    );
    expect(field?.title).toBe("API Base URL");
  });

  it("strips a trailing standalone Config/Settings word", () => {
    const field = sectionOf("embeddings").fields.find(
      (f) => f.key === "retry_config"
    );
    expect(field?.title).toBe("Retry");
  });

  it("drops deprecated fields entirely", () => {
    const keys = sectionOf("embeddings").fields.map((f) => f.key);
    expect(keys).not.toContain("legacy_flag");
  });
});

// --- search ----------------------------------------------------------------

describe("SettingsModel.search", () => {
  it("matches a section title and returns its section + group ids", () => {
    const { sectionIds, groupIds } = model().search("embeddings");
    expect(sectionIds.has("embeddings")).toBe(true);
    expect(groupIds.has("models")).toBe(true);
  });

  it("matches a field title, case-insensitively", () => {
    const { sectionIds } = model().search("API KEY");
    expect(sectionIds.has("llm")).toBe(true);
  });

  it("matches a field description", () => {
    const { sectionIds } = model().search("which chat model");
    expect(sectionIds.has("llm")).toBe(true);
  });

  it("returns empty sets for a blank or non-matching query", () => {
    expect(model().search("   ").sectionIds.size).toBe(0);
    expect(model().search("zzzzz").sectionIds.size).toBe(0);
  });
});

// --- isDirty / patchFor ----------------------------------------------------

describe("SettingsModel.isDirty / patchFor", () => {
  it("reports clean when form data equals the original config", () => {
    const unchanged = { model: "gpt-5", api_key: "", proxy_model_prefix: "openai" };
    expect(model().isDirty("llm", unchanged)).toBe(false);
    expect(model().patchFor("llm", unchanged)).toBeNull();
  });

  it("emits a section-scoped patch for a top-level scalar change", () => {
    const patch = model().patchFor("llm", {
      model: "claude-opus-4-8",
      api_key: "",
      proxy_model_prefix: "openai",
    });
    expect(patch).toEqual({ llm: { model: "claude-opus-4-8" } });
    expect(model().isDirty("llm", { model: "claude-opus-4-8" })).toBe(true);
  });

  it("emits a nested diff shape for a nested change", () => {
    const patch = model().patchFor("agent", {
      edit_tool: "",
      lsp: { enabled: false },
    });
    expect(patch).toEqual({ agent: { lsp: { enabled: false } } });
  });

  it("unchanged nested object → no key", () => {
    // Only edit_tool changes; lsp is untouched → lsp must NOT appear.
    const patch = model().patchFor("agent", {
      edit_tool: "structured_patch",
      lsp: { enabled: true },
    });
    expect(patch).toEqual({ agent: { edit_tool: "structured_patch" } });
  });
});

// --- sliceSchema -----------------------------------------------------------

describe("SettingsModel.sliceSchema", () => {
  it("returns the section's def as a root schema carrying $defs", () => {
    const slice = model().sliceSchema("agent");
    expect(slice.type).toBe("object");
    expect(slice.title).toBe("Agent");
    // nested $ref to LspConfig must still resolve → $defs carried through.
    expect(slice.$defs).toBe(schema.$defs);
    const props = slice.properties as Record<string, unknown>;
    expect(props).toHaveProperty("lsp");
  });

  it("uses the inline property schema as root for non-$ref sections", () => {
    const slice = model().sliceSchema("channels");
    expect(slice.type).toBe("object");
    expect(slice.title).toBe("Channels");
    expect(slice.properties).toHaveProperty("nextcloud_url");
  });
});

// --- uiSchemaFor (widget routing) ------------------------------------------

/**
 * A compact AppConfig-shaped schema exercising every uiSchemaFor branch:
 *   - `projects` — whole-section dict (additionalProperties, no properties).
 *   - `plugins` — `hooks` array-of-object (recordList), `marketplaces` scalar
 *     array (marketplace itemValidator), `secret_token` secret field.
 *   - `agent` — `plan_mode_shell_allowlist` scalar array (restoreDefault),
 *     `model_context_windows` dict[str,int] (keyedCollection), nested `lsp`
 *     submodel whose `servers` is a dict (keyedCollection).
 */
const uiSchema: Record<string, unknown> = {
  type: "object",
  title: "AppConfig",
  properties: {
    projects: { $ref: "#/$defs/ProjectsMap" },
    plugins: { $ref: "#/$defs/PluginsConfig" },
    agent: { $ref: "#/$defs/AgentConfig" },
  },
  $defs: {
    // Whole-section dict: additionalProperties + no properties.
    ProjectsMap: {
      type: "object",
      title: "Projects",
      additionalProperties: { $ref: "#/$defs/ProjectConfig" },
    },
    ProjectConfig: {
      type: "object",
      title: "ProjectConfig",
      properties: { path: { type: "string", title: "Path" } },
    },
    PluginsConfig: {
      type: "object",
      title: "Plugins",
      properties: {
        hooks: {
          type: "array",
          title: "Hooks",
          items: { $ref: "#/$defs/HookEntry" },
        },
        marketplaces: {
          type: "array",
          title: "Marketplaces",
          items: { type: "string" },
        },
        secret_token: { type: "string", title: "Token", writeOnly: true },
      },
    },
    HookEntry: {
      type: "object",
      title: "HookEntry",
      properties: { command: { type: "string", title: "Command" } },
    },
    AgentConfig: {
      type: "object",
      title: "Agent",
      properties: {
        plan_mode_shell_allowlist: {
          type: "array",
          title: "Plan Mode Shell Allowlist",
          items: { type: "string" },
          default: ["ls", "cat"],
        },
        model_context_windows: {
          type: "object",
          title: "Model Context Windows",
          additionalProperties: { type: "integer" },
        },
        lsp: { $ref: "#/$defs/LspConfig" },
      },
    },
    LspConfig: {
      type: "object",
      title: "LSP",
      properties: {
        enabled: { type: "boolean", title: "Enabled" },
        servers: {
          type: "object",
          title: "Servers",
          additionalProperties: { $ref: "#/$defs/LspServer" },
        },
      },
    },
    LspServer: {
      type: "object",
      title: "LspServer",
      properties: { command: { type: "string", title: "Command" } },
    },
  },
};

const uiModel = () => new SettingsModel(uiSchema, {});

describe("SettingsModel.uiSchemaFor", () => {
  it("routes a whole-dict section to keyedCollection", () => {
    expect(uiModel().uiSchemaFor("projects", {}, 0)).toEqual({
      "ui:field": "keyedCollection",
    });
  });

  it("routes an array-of-object property to recordList", () => {
    const ui = uiModel().uiSchemaFor("plugins", {}, 0) as Record<
      string,
      Record<string, unknown>
    >;
    expect(ui.hooks).toEqual({ "ui:field": "recordList" });
  });

  it("routes a dict[str, int] property to keyedCollection", () => {
    const ui = uiModel().uiSchemaFor("agent", {}, 0) as Record<
      string,
      Record<string, unknown>
    >;
    expect(ui.model_context_windows).toEqual({
      "ui:field": "keyedCollection",
    });
  });

  it("wires plugins.marketplaces with the marketplace itemValidator fn", () => {
    const ui = uiModel().uiSchemaFor("plugins", {}, 0) as Record<
      string,
      { "ui:options"?: { itemValidator?: unknown } }
    >;
    expect(typeof ui.marketplaces?.["ui:options"]?.itemValidator).toBe(
      "function"
    );
  });

  it("wires agent.plan_mode_shell_allowlist with restoreDefault", () => {
    const ui = uiModel().uiSchemaFor("agent", {}, 0) as Record<
      string,
      { "ui:options"?: { restoreDefault?: unknown } }
    >;
    expect(ui.plan_mode_shell_allowlist?.["ui:options"]?.restoreDefault).toBe(
      true
    );
  });

  it("routes a secret field to the secret widget with secretConfigured", () => {
    const ui = uiModel().uiSchemaFor(
      "plugins",
      { "plugins.secret_token": true },
      42
    ) as Record<string, Record<string, unknown>>;
    expect(ui.secret_token).toEqual({
      "ui:widget": "secret",
      "ui:options": { secretConfigured: true, savedAt: 42 },
    });
  });

  it("recurses a nested submodel and routes its dict child to keyedCollection", () => {
    const ui = uiModel().uiSchemaFor("agent", {}, 0) as Record<
      string,
      Record<string, Record<string, unknown>>
    >;
    expect(ui.lsp.servers["ui:field"]).toBe("keyedCollection");
  });
});
