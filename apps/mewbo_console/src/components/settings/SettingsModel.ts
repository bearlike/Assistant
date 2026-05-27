/**
 * SettingsModel — the pure, React-free heart of the faceted Settings UI.
 *
 * The backend serves `AppConfig.model_json_schema()`. This class is the
 * single source of truth that turns that raw JSON schema (+ the current
 * config values) into the structure the Settings shell renders:
 *
 *   schema.$defs.<Class>  →  facet metadata (x-group / x-order / x-advanced)
 *   schema.properties.<k> →  one config section, resolved through its $ref
 *
 * It buckets sections into the six presentation facets (see `facets.ts`),
 * slices a standalone RJSF root schema per section, drives search, and computes
 * section-scoped patches via its canonical `recursiveDiff`.
 *
 * Convention (atomic class): the parsed groups live as frozen state on the
 * instance, behaviour is on the prototype, and the diff core is a private
 * static helper. No scattered module-level functions own settings logic.
 */
import {
  FACETS,
  FALLBACK_FACET_ID,
  type FacetMeta,
} from "./facets";
import { validateMarketplaceEntry } from "./marketplaceValidation";

// ---------------------------------------------------------------------------
// Public node types
// ---------------------------------------------------------------------------

/** A single leaf/scalar field inside a section (for search + visibility). */
export interface FieldNode {
  /** Property key, e.g. "api_key". Sections are flat, so this is the leaf id. */
  key: string;
  /** Human label — schema `title`, else prettified `key`. */
  title: string;
  description?: string;
  /** `x-advanced` on the field. */
  advanced: boolean;
  /** `x-secret` OR `writeOnly` on the field. */
  secret: boolean;
  /** `deprecated` on the field — these are excluded from the rendered form. */
  deprecated: boolean;
  /** Stable display order (schema order; bare index when unspecified). */
  order: number;
}

/** A top-level AppConfig section (one submodel or inline object). */
export interface SectionNode {
  /** Top-level AppConfig key, e.g. "llm". */
  id: string;
  title: string;
  description?: string;
  /** Facet id this section belongs to. */
  groupId: string;
  /** `x-order` within the facet (Infinity when unspecified → sorts last). */
  order: number;
  /** Section-level `x-advanced`. */
  advanced: boolean;
  /** Top-level scalar/leaf fields of the section. */
  fields: FieldNode[];
}

/** A presentation facet plus its resolved, ordered sections. */
export interface GroupNode {
  id: string;
  title: string;
  iconName: string;
  order: number;
  sections: SectionNode[];
}

// ---------------------------------------------------------------------------
// Internal schema shapes (narrow, only what we read)
// ---------------------------------------------------------------------------

type JsonObject = Record<string, unknown>;

const isObject = (v: unknown): v is JsonObject =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const asString = (v: unknown): string | undefined =>
  typeof v === "string" ? v : undefined;

const asBool = (v: unknown): boolean => v === true;

const asNumber = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;

/**
 * Acronyms that should render fully uppercased, not Title Cased. Matched
 * per-word, case-insensitively. Source of truth for the prettify fallback.
 */
const ACRONYMS = new Set([
  "API", "URL", "HTTP", "HTTPS", "ID", "LLM", "CLI", "LSP", "MCP", "IDE",
  "HA", "UI", "JSON", "TTL", "SCG", "QA",
]);

/**
 * Prettify a snake/camel key into a Title Case label. Used ONLY as a fallback
 * when a schema carries no `title`. Beyond Title Casing it (a) uppercases known
 * acronyms and (b) strips a trailing standalone `Config`/`Settings` word
 * (`LlmConfig` → `LLM`, `web_ide_settings` → `Web IDE`). Pure function.
 */
function prettify(key: string): string {
  const titled = key
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());

  const words = titled
    .split(/\s+/)
    .filter(Boolean)
    .map((w) => (ACRONYMS.has(w.toUpperCase()) ? w.toUpperCase() : w));

  // Strip a trailing standalone Config/Settings word (keep it if it's the
  // only word, so a section literally named "Config" survives).
  if (words.length > 1) {
    const last = words[words.length - 1].toLowerCase();
    if (last === "config" || last === "settings") words.pop();
  }

  return words.join(" ");
}

// ---------------------------------------------------------------------------
// SettingsModel
// ---------------------------------------------------------------------------

export class SettingsModel {
  /** The raw AppConfig JSON schema as served by the backend. */
  private readonly schema: JsonObject;
  /** Current config values keyed by section id. */
  private readonly config: JsonObject;
  /** `schema.$defs`, used to resolve `$ref`s. */
  private readonly defs: JsonObject;
  /** Ordered facet groups with their sections — computed once at construction. */
  private readonly _groups: GroupNode[];
  /** Flat section lookup by id. */
  private readonly _sectionById: Map<string, SectionNode>;

  constructor(
    schema: Record<string, unknown>,
    config: Record<string, unknown>
  ) {
    this.schema = schema;
    this.config = config;
    this.defs = isObject(schema.$defs) ? schema.$defs : {};

    const sections = this.buildSections();
    this._sectionById = new Map(sections.map((s) => [s.id, s]));
    this._groups = SettingsModel.bucketIntoFacets(sections);
  }

  // -- construction helpers -------------------------------------------------

  /**
   * Resolve a top-level section property to its effective object schema.
   * Follows a single `$ref` into `$defs`; falls back to the inline property
   * schema for sections declared inline (no `$ref`).
   */
  private resolveSectionSchema(prop: JsonObject): {
    def: JsonObject;
    refName?: string;
  } {
    const ref = asString(prop.$ref);
    if (ref) {
      const refName = ref.replace(/^#\/\$defs\//, "");
      const def = this.defs[refName];
      if (isObject(def)) return { def, refName };
    }
    return { def: prop };
  }

  /**
   * Parse the scalar/leaf fields of a section's object schema. Deprecated
   * fields are dropped entirely so they never reach search or the form.
   */
  private static buildFields(def: JsonObject): FieldNode[] {
    const props = isObject(def.properties) ? def.properties : {};
    return Object.keys(props)
      .map((key, index) => {
        const fieldSchema = isObject(props[key]) ? (props[key] as JsonObject) : {};
        return {
          key,
          title: asString(fieldSchema.title) ?? prettify(key),
          description: asString(fieldSchema.description),
          advanced: asBool(fieldSchema["x-advanced"]),
          secret:
            asBool(fieldSchema["x-secret"]) || asBool(fieldSchema.writeOnly),
          deprecated: asBool(fieldSchema.deprecated),
          order: asNumber(fieldSchema["x-order"]) ?? index,
        };
      })
      .filter((field) => !field.deprecated);
  }

  /** Build every top-level section from `schema.properties`. */
  private buildSections(): SectionNode[] {
    const props = isObject(this.schema.properties)
      ? this.schema.properties
      : {};
    const sections: SectionNode[] = [];
    for (const id of Object.keys(props)) {
      const prop = isObject(props[id]) ? (props[id] as JsonObject) : {};
      const { def } = this.resolveSectionSchema(prop);
      const groupId = asString(def["x-group"]);
      sections.push({
        id,
        // Prefer the section's own description (sibling of the $ref) but
        // fall back to the def's; title comes from the def, then the key.
        title: asString(def.title) ?? prettify(id),
        description: asString(prop.description) ?? asString(def.description),
        groupId:
          groupId && FACETS.some((f) => f.id === groupId)
            ? groupId
            : FALLBACK_FACET_ID,
        order: asNumber(def["x-order"]) ?? Infinity,
        advanced: asBool(def["x-advanced"]),
        fields: SettingsModel.buildFields(def),
      });
    }
    return sections;
  }

  /** Bucket sections into the ordered facet list (empty facets included). */
  private static bucketIntoFacets(sections: SectionNode[]): GroupNode[] {
    const byFacet = new Map<string, SectionNode[]>();
    for (const facet of FACETS) byFacet.set(facet.id, []);
    for (const section of sections) {
      const bucket = byFacet.get(section.groupId) ?? byFacet.get(FALLBACK_FACET_ID);
      bucket?.push(section);
    }
    return FACETS.map((facet: FacetMeta) => ({
      id: facet.id,
      title: facet.title,
      iconName: facet.iconName,
      order: facet.order,
      sections: (byFacet.get(facet.id) ?? []).sort(SettingsModel.compareSections),
    }));
  }

  /** Deterministic section ordering: x-order, then key. */
  private static compareSections(a: SectionNode, b: SectionNode): number {
    if (a.order !== b.order) return a.order - b.order;
    return a.id.localeCompare(b.id);
  }

  // -- public API -----------------------------------------------------------

  /**
   * Ordered facets → ordered sections. Empty facets (including `security`,
   * which has no schema sections) are included so the shell can render them.
   */
  groups(): GroupNode[] {
    return this._groups;
  }

  /** Look up a section by its top-level AppConfig id. */
  section(id: string): SectionNode | undefined {
    return this._sectionById.get(id);
  }

  /**
   * Build a standalone RJSF root schema for a single section: the section's
   * resolved def as the root, carrying `$defs` so nested `$ref`s resolve.
   * Inline sections (no `$ref`) use their inline property schema as the root.
   */
  sliceSchema(id: string): Record<string, unknown> {
    const props = isObject(this.schema.properties)
      ? this.schema.properties
      : {};
    const prop = isObject(props[id]) ? (props[id] as JsonObject) : {};
    const { def } = this.resolveSectionSchema(prop);
    const section = this._sectionById.get(id);
    return {
      ...def,
      title: section?.title ?? asString(def.title) ?? prettify(id),
      ...(isObject(this.schema.$defs) ? { $defs: this.schema.$defs } : {}),
    };
  }

  // -- ui:schema (widget routing) -------------------------------------------

  /** Resolve a single-level `$ref` into `$defs`; passthrough otherwise. */
  private resolveRef(schema: JsonObject): JsonObject {
    const ref = asString(schema.$ref);
    if (ref) {
      const def = this.defs[ref.replace(/^#\/\$defs\//, "")];
      if (isObject(def)) return def;
    }
    return schema;
  }

  /**
   * A "dict" schema is an object with `additionalProperties` (true or a value
   * schema) and no declared `properties` — i.e. a `dict[str, X]` config map
   * rather than a fixed-shape submodel.
   */
  private static isDictSchema(s: JsonObject): boolean {
    const ap = s.additionalProperties;
    const hasProps =
      isObject(s.properties) && Object.keys(s.properties).length > 0;
    return !hasProps && (ap === true || isObject(ap));
  }

  /**
   * Build the RJSF `ui:schema` for a section: routes custom widgets/fields
   * (secret, recordList, keyedCollection) and per-array `ui:options`
   * (marketplace validation, restore-defaults). A whole-section dict (e.g.
   * `projects`, `channels`) routes the entire section to `keyedCollection`;
   * otherwise it recurses the section's properties.
   *
   * `secrets` is the full dot-path → is-set map and `savedAt` bumps on each
   * successful Save so SecretField can leave its editing state once persisted.
   */
  uiSchemaFor(
    id: string,
    secrets: Record<string, boolean>,
    savedAt: number
  ): Record<string, unknown> {
    const sliced = this.sliceSchema(id) as JsonObject;
    if (SettingsModel.isDictSchema(sliced)) {
      return { "ui:field": "keyedCollection" };
    }
    const ui: Record<string, unknown> = {};
    this.buildObjectUi(sliced, ui, id, [], secrets, savedAt);
    return ui;
  }

  /**
   * Recurse a fixed-shape object schema, populating `ui` with the widget/field
   * routing for each property. `path` tracks the dot-path from the section root
   * (for the secret `secrets` lookup); `sectionId` selects the section-specific
   * array behaviours (marketplaces validation, plan-mode restore-defaults).
   */
  private buildObjectUi(
    objSchema: JsonObject,
    ui: Record<string, unknown>,
    sectionId: string,
    path: string[],
    secrets: Record<string, boolean>,
    savedAt: number
  ): void {
    const props = isObject(objSchema.properties) ? objSchema.properties : {};
    for (const key of Object.keys(props)) {
      const raw = isObject(props[key]) ? (props[key] as JsonObject) : {};
      const schema = this.resolveRef(raw);

      // Secret (write-only) field — flagged on the property or its resolved def.
      const secret =
        asBool(raw["x-secret"]) ||
        asBool(raw.writeOnly) ||
        asBool(schema["x-secret"]) ||
        asBool(schema.writeOnly);
      if (secret) {
        const dot = [...path, key].join(".");
        ui[key] = {
          "ui:widget": "secret",
          "ui:options": {
            secretConfigured: secrets[`${sectionId}.${dot}`] ?? false,
            savedAt,
          },
        };
        continue;
      }

      const type = asString(schema.type) ?? asString(raw.type);
      if (type === "array") {
        const items = isObject(schema.items)
          ? this.resolveRef(schema.items as JsonObject)
          : {};
        if (asString(items.type) === "object") {
          // list[BaseModel] (e.g. the HooksConfig arrays) → record-list editor.
          ui[key] = { "ui:field": "recordList" };
        } else if (sectionId === "plugins" && key === "marketplaces") {
          ui[key] = { "ui:options": { itemValidator: validateMarketplaceEntry } };
        } else if (sectionId === "agent" && key === "plan_mode_shell_allowlist") {
          ui[key] = { "ui:options": { restoreDefault: true } };
        }
        // Other scalar arrays use the default ArrayFieldTemplate — no entry.
      } else if (type === "object") {
        if (SettingsModel.isDictSchema(schema)) {
          // dict[str, X] (e.g. model_context_windows, lsp.servers) → keyed map.
          ui[key] = { "ui:field": "keyedCollection" };
        } else {
          const childUi: Record<string, unknown> = {};
          this.buildObjectUi(
            schema,
            childUi,
            sectionId,
            [...path, key],
            secrets,
            savedAt
          );
          if (Object.keys(childUi).length > 0) ui[key] = childUi;
        }
      }
    }
  }

  /**
   * Case-insensitive search across section/field title, description, and
   * field path. Returns the ids of matching sections and their facets so
   * the shell can filter both the sidebar and the section list.
   */
  search(query: string): { groupIds: Set<string>; sectionIds: Set<string> } {
    const groupIds = new Set<string>();
    const sectionIds = new Set<string>();
    const q = query.trim().toLowerCase();
    if (!q) return { groupIds, sectionIds };

    for (const group of this._groups) {
      for (const section of group.sections) {
        if (
          SettingsModel.matches(q, section.title) ||
          SettingsModel.matches(q, section.description) ||
          SettingsModel.matches(q, section.id) ||
          section.fields.some(
            (f) =>
              SettingsModel.matches(q, f.title) ||
              SettingsModel.matches(q, f.description) ||
              SettingsModel.matches(q, f.key)
          )
        ) {
          sectionIds.add(section.id);
          groupIds.add(group.id);
        }
      }
    }
    return { groupIds, sectionIds };
  }

  private static matches(needle: string, haystack?: string): boolean {
    return haystack ? haystack.toLowerCase().includes(needle) : false;
  }

  /** True when a section's form data differs from the original config. */
  isDirty(id: string, formData: unknown): boolean {
    return this.patchFor(id, formData) !== null;
  }

  /**
   * Section-scoped patch: `{ [id]: <recursive diff> }`, or `null` when
   * nothing changed. `recursiveDiff` is the canonical section-diff so the model
   * and the save path always agree on what counts as a change.
   */
  patchFor(id: string, formData: unknown): Record<string, unknown> | null {
    const original = isObject(this.config[id])
      ? (this.config[id] as JsonObject)
      : {};
    const updated = isObject(formData) ? formData : {};
    const diff = SettingsModel.recursiveDiff(original, updated);
    return diff ? { [id]: diff } : null;
  }

  // -- diff core (canonical section-diff) -----------------------------------

  /**
   * Recursive diff: only keys whose values changed, recursing into plain
   * objects and comparing everything else by JSON value. The single source of
   * truth for what a section-scoped settings patch contains.
   */
  private static recursiveDiff(
    original: JsonObject,
    updated: JsonObject
  ): JsonObject | null {
    const diff: JsonObject = {};
    let hasChange = false;
    for (const key of Object.keys(updated)) {
      const origVal = original[key];
      const newVal = updated[key];
      if (isObject(origVal) && isObject(newVal)) {
        const nested = SettingsModel.recursiveDiff(origVal, newVal);
        if (nested) {
          diff[key] = nested;
          hasChange = true;
        }
      } else if (JSON.stringify(origVal) !== JSON.stringify(newVal)) {
        diff[key] = newVal;
        hasChange = true;
      }
    }
    return hasChange ? diff : null;
  }
}
