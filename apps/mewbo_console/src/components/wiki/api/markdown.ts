/**
 * Markdown + YAML frontmatter parser.
 *
 * Splits a raw `.md` source string into structured frontmatter + body and
 * auto-derives the TOC from `##`/`###` headings (with `tocOverride` as the
 * escape hatch). The renderer (`MarkdownBlock.tsx`) consumes the body via
 * `react-markdown`.
 *
 * Heading slugs use a tiny, dependency-free GitHub-style slugifier so the
 * TOC ids match `rehype-slug`'s output exactly.
 */

import yaml from "js-yaml";

import type { TocEntry } from "./types";

// gray-matter pulls in Node's Buffer, which doesn't exist in the browser.
// We split frontmatter ourselves and let `js-yaml` (browser-safe) parse the
// header. The contract is identical to `gray-matter`'s `data` + `content`.
const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

export interface PageSourceParsed {
  frontmatter: PageFrontmatter;
  body: string;
  toc: TocEntry[];
}

export interface PageFrontmatter {
  title: string;
  slug: string;
  relevantSources?: Array<{ path: string; lines?: string }>;
  sources?: Array<{ path: string; lines?: string }>;
  tocOverride?: TocEntry[];
}

/**
 * `gray-matter` strips frontmatter and returns `data` + `content`. We
 * shape that into our `PageSourceParsed` and auto-derive the TOC.
 */
export function parsePageSource(raw: string): PageSourceParsed {
  const m = raw.match(FRONTMATTER_RE);
  const data = (m ? (yaml.load(m[1]) as Partial<PageFrontmatter> | null) : null) ?? {};
  const body = (m ? raw.slice(m[0].length) : raw).replace(/^\n+/, "");
  const fm: PageFrontmatter = {
    title: String(data.title ?? "Untitled"),
    slug: String(data.slug ?? "untitled"),
    relevantSources: normaliseSourceList(data.relevantSources),
    sources: normaliseSourceList(data.sources),
    tocOverride: normaliseToc(data.tocOverride),
  };
  const toc = fm.tocOverride ?? deriveToc(fm.title, body);
  return { frontmatter: fm, body, toc };
}

function normaliseSourceList(value: unknown): Array<{ path: string; lines?: string }> | undefined {
  if (!Array.isArray(value)) return undefined;
  return value
    .map((entry) => {
      if (typeof entry === "string") return { path: entry };
      if (entry && typeof entry === "object") {
        const path = (entry as { path?: unknown }).path;
        const lines = (entry as { lines?: unknown }).lines;
        if (typeof path === "string") {
          return { path, lines: typeof lines === "string" ? lines : undefined };
        }
      }
      return null;
    })
    .filter((x): x is { path: string; lines?: string } => x !== null);
}

function normaliseToc(value: unknown): TocEntry[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value
    .map((entry) => {
      if (!entry || typeof entry !== "object") return null;
      const o = entry as { id?: unknown; label?: unknown; lvl?: unknown };
      if (typeof o.id !== "string" || typeof o.label !== "string") return null;
      const lvl = o.lvl === 1 || o.lvl === 2 || o.lvl === 3 ? o.lvl : 2;
      return { id: o.id, label: o.label, lvl: lvl as 1 | 2 | 3 };
    })
    .filter((x): x is TocEntry => x !== null);
}

/**
 * Walk the markdown body line-by-line, capturing `## ` and `### ` headings
 * (ignoring fences so headings inside code blocks don't pollute the TOC).
 * The page title is always the first entry (`#page-top`).
 */
function deriveToc(title: string, body: string): TocEntry[] {
  const out: TocEntry[] = [{ id: "page-top", label: title, lvl: 1 }];
  let inFence = false;
  for (const line of body.split("\n")) {
    if (/^```/.test(line.trim())) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const h2 = line.match(/^##\s+(.+?)\s*$/);
    if (h2) {
      out.push({ id: slugify(h2[1]), label: h2[1], lvl: 2 });
      continue;
    }
    const h3 = line.match(/^###\s+(.+?)\s*$/);
    if (h3) {
      out.push({ id: slugify(h3[1]), label: h3[1], lvl: 3 });
    }
  }
  return out;
}

/**
 * GitHub-style slugifier. Matches what `rehype-slug` writes onto heading
 * elements, so TOC anchors line up with rendered ids without extra wiring.
 */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-");
}
