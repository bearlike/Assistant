/**
 * Citation grammar — the single parser/normalizer for the Q&A source
 * citation strings that flow through both the answer prose (inline ``src:``
 * links / chips) and the right-panel source cards.
 *
 * Grammar handled:
 *   - ``path``                     → a whole-file citation
 *   - ``path#L<start>-<end>``      → a line-range citation
 *   - ``path#L<n>``                → a single-line citation
 *   - ``path:line`` / ``path:a-b`` → terse colon form (chip text + bare-text fallback)
 *   - ``graph:<node_id>`` / ``wiki:<page_id>`` → provenance refs (NOT file cards)
 *
 * One atomic class so the DOM id a card mounts under and the id an inline
 * chip scrolls to are computed by the exact same code — no drift, no second
 * normalizer. The id form is the canonical ``path#L<start>-<end>`` (or bare
 * ``path`` when no range) prefixed with ``src-`` for a valid DOM token.
 */

/** Parsed shape of a single citation string. */
export interface Citation {
  /** Original, untouched citation string. */
  raw: string;
  /** Scheme prefix when present (``graph`` / ``wiki``); ``null`` for file refs. */
  scheme: "graph" | "wiki" | null;
  /** File path (only meaningful when ``scheme === null``). */
  path: string;
  /** 1-based first line of the range, or ``null`` for a whole-file ref. */
  startLine: number | null;
  /** 1-based last line of the range, or ``null``. */
  endLine: number | null;
  /** ``true`` when this is a file-source citation (renderable as a card). */
  isFileSource: boolean;
}

/** A valid DOM-id character set so ``getElementById`` round-trips cleanly. */
function toDomToken(s: string): string {
  return s.replace(/[^a-zA-Z0-9_-]/g, "_");
}

/**
 * Stable DOM id for a file source (path + optional line range), shared by the
 * legacy {@link CitationRef.domId} and the discriminated {@link citationDomId}
 * so a card and its inline ``src:`` chip always resolve to the same node.
 */
function fileDomId(path: string, startLine: number | null, endLine: number | null): string {
  const range = startLine != null
    ? `#L${startLine}${endLine != null && endLine !== startLine ? `-${endLine}` : ""}`
    : "";
  return `src-${toDomToken(`${path}${range}`)}`;
}

/** Short ``path:line`` (or ``path:a–b``) label for a file source. */
function fileLabel(path: string, startLine: number | null, endLine: number | null): string {
  if (startLine == null) return path;
  const range = endLine != null && endLine !== startLine
    ? `${startLine}–${endLine}`
    : `${startLine}`;
  return `${path}:${range}`;
}

export class CitationRef {
  /**
   * Parse one citation string into a {@link Citation}. Tolerant: anything
   * that doesn't match a known shape becomes a whole-file ref on the raw
   * string, so the parser never throws and never drops data.
   */
  static parse(raw: string): Citation {
    const trimmed = raw.trim();

    // Provenance schemes — not file sources, no line info.
    if (trimmed.startsWith("graph:")) {
      return {
        raw,
        scheme: "graph",
        path: trimmed.slice("graph:".length),
        startLine: null,
        endLine: null,
        isFileSource: false,
      };
    }
    if (trimmed.startsWith("wiki:")) {
      return {
        raw,
        scheme: "wiki",
        path: trimmed.slice("wiki:".length),
        startLine: null,
        endLine: null,
        isFileSource: false,
      };
    }

    // ``path#L<a>-<b>`` / ``path#L<n>`` (canonical) OR ``path#<a>-<b>``.
    const hashIdx = trimmed.indexOf("#");
    if (hashIdx !== -1) {
      const path = trimmed.slice(0, hashIdx);
      const frag = trimmed.slice(hashIdx + 1);
      const { start, end } = CitationRef._parseRange(frag);
      return {
        raw,
        scheme: null,
        path,
        startLine: start,
        endLine: end,
        isFileSource: Boolean(path),
      };
    }

    // ``path:line`` / ``path:a-b`` colon form. Guard against drive-letter /
    // scheme-ish colons by requiring the suffix to start with a digit.
    const colonIdx = trimmed.lastIndexOf(":");
    if (colonIdx > 0 && /^\d/.test(trimmed.slice(colonIdx + 1))) {
      const path = trimmed.slice(0, colonIdx);
      const { start, end } = CitationRef._parseRange(trimmed.slice(colonIdx + 1));
      return {
        raw,
        scheme: null,
        path,
        startLine: start,
        endLine: end,
        isFileSource: Boolean(path),
      };
    }

    // Bare path — whole-file source.
    return {
      raw,
      scheme: null,
      path: trimmed,
      startLine: null,
      endLine: null,
      isFileSource: Boolean(trimmed),
    };
  }

  /**
   * Build a {@link Citation} from a structured ``src`` inline atom
   * (``{ path, lines }``) so the inline-chip path and the card path agree.
   * ``lines`` follows the same ``L<a>-<b>`` / ``a-b`` grammar as the URL frag.
   */
  static fromSrc(path: string, lines?: string): Citation {
    const { start, end } = lines ? CitationRef._parseRange(lines) : { start: null, end: null };
    return {
      raw: lines ? `${path}#L${lines.replace(/^L/, "")}` : path,
      scheme: null,
      path,
      startLine: start,
      endLine: end,
      isFileSource: Boolean(path),
    };
  }

  /**
   * Stable DOM id a {@link SourceCard} mounts under and an inline chip
   * scrolls to. Derived purely from path + range so both ends agree without
   * prop threading. Whole-file refs collapse onto the same card id.
   */
  static domId(c: Citation): string {
    return fileDomId(c.path, c.startLine, c.endLine);
  }

  /** Short ``path:line`` label for chips / card headers (no scheme prefix). */
  static label(c: Citation): string {
    if (c.scheme) return c.path;
    return fileLabel(c.path, c.startLine, c.endLine);
  }

  /** Dedup key — two citations to the same path+range collapse into one card. */
  static key(c: Citation): string {
    return CitationRef.domId(c);
  }

  private static _parseRange(frag: string): { start: number | null; end: number | null } {
    // Strip a leading ``L`` (``L12-44`` → ``12-44``), then split a-b.
    const body = frag.replace(/^L/, "");
    const m = /^(\d+)(?:-L?(\d+))?$/.exec(body);
    if (!m) return { start: null, end: null };
    const start = Number(m[1]);
    const end = m[2] != null ? Number(m[2]) : start;
    return {
      start: Number.isFinite(start) ? start : null,
      end: Number.isFinite(end) ? end : null,
    };
  }
}

/**
 * Parse a list of raw citation strings into the unique set of file-source
 * cards (in first-seen order), dropping provenance schemes and dups. This is
 * the card-set builder QAScreen feeds the right panel.
 */
export function fileCitations(raws: Iterable<string>): Citation[] {
  const seen = new Set<string>();
  const out: Citation[] = [];
  for (const raw of raws) {
    if (!raw) continue;
    const c = CitationRef.parse(raw);
    if (!c.isFileSource) continue;
    const k = CitationRef.key(c);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(c);
  }
  return out;
}

/**
 * A citation classified by how its card should render. Unlike the flat
 * {@link Citation} (which collapses every kind onto a file path), this keeps
 * the three distinct shapes the answer can cite:
 *
 *   - ``file``  — a repo file excerpt (lazily fetched + line-highlighted).
 *   - ``page``  — a documentation page, fetched once by id (title + excerpt).
 *   - ``graph`` — a code-graph node, rendered as a resolved label (no fetch).
 *
 * An optional ``|<display>`` suffix on the raw ref carries a human label
 * (``wiki:home|Home Page`` / ``graph:mod.py::Thing|Thing (class)``) — file
 * paths never contain ``|`` so the delimiter is unambiguous.
 */
export type ParsedCitation =
  | { kind: "file"; raw: string; path: string; startLine: number | null; endLine: number | null }
  | { kind: "page"; raw: string; pageId: string; title?: string }
  | { kind: "graph"; raw: string; nodeId: string; label?: string };

/** Split an optional trailing ``|<display>`` label off a raw ref. */
function splitDisplay(ref: string): { head: string; display?: string } {
  const i = ref.indexOf("|");
  if (i === -1) return { head: ref };
  const display = ref.slice(i + 1).trim();
  return { head: ref.slice(0, i), display: display || undefined };
}

/** Compact a graph node id to its trailing symbol (``mod.py::Thing`` → ``Thing``). */
function compactNodeLabel(nodeId: string): string {
  const sym = nodeId.split(/::|#/).pop();
  return sym && sym.length ? sym : nodeId;
}

/** Parse one raw ref into its discriminated shape. */
function parseOneCitation(ref: string): ParsedCitation {
  const { head, display } = splitDisplay(ref);
  const c = CitationRef.parse(head);
  if (c.scheme === "wiki") {
    return { kind: "page", raw: ref, pageId: c.path, title: display };
  }
  if (c.scheme === "graph") {
    return { kind: "graph", raw: ref, nodeId: c.path, label: display };
  }
  return { kind: "file", raw: ref, path: c.path, startLine: c.startLine, endLine: c.endLine };
}

/**
 * Parse a list of raw citation strings into the unique, kind-tagged card set
 * (first-seen order, dups + empties dropped). Superset of {@link fileCitations}
 * — it KEEPS the ``wiki:`` page and ``graph:`` node refs that the answer made,
 * instead of silently discarding them.
 */
export function parseCitations(raws: Iterable<string>): ParsedCitation[] {
  const seen = new Set<string>();
  const out: ParsedCitation[] = [];
  for (const raw of raws) {
    if (!raw) continue;
    const c = parseOneCitation(raw);
    const k = citationDomId(c);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(c);
  }
  return out;
}

/**
 * Coerce a legacy flat {@link Citation} OR a {@link ParsedCitation} into the
 * discriminated form, so {@link SourceCard} renders either without its callers
 * (e.g. the not-yet-migrated QAScreen, which still feeds flat file citations)
 * having to change.
 */
export function asParsedCitation(c: Citation | ParsedCitation): ParsedCitation {
  if ("kind" in c) return c;
  if (c.scheme === "wiki") return { kind: "page", raw: c.raw, pageId: c.path };
  if (c.scheme === "graph") return { kind: "graph", raw: c.raw, nodeId: c.path };
  return { kind: "file", raw: c.raw, path: c.path, startLine: c.startLine, endLine: c.endLine };
}

/**
 * Stable DOM id + dedup key for a parsed citation. File ids match
 * {@link CitationRef.domId} so an inline ``src:`` chip still scrolls to its card.
 */
export function citationDomId(c: ParsedCitation): string {
  switch (c.kind) {
    case "file":
      return fileDomId(c.path, c.startLine, c.endLine);
    case "page":
      return `src-${toDomToken(`wiki:${c.pageId}`)}`;
    case "graph":
      return `src-${toDomToken(`graph:${c.nodeId}`)}`;
  }
}

/** Header label for a parsed citation card (title/label fall back to the id). */
export function citationLabel(c: ParsedCitation): string {
  switch (c.kind) {
    case "file":
      return fileLabel(c.path, c.startLine, c.endLine);
    case "page":
      return c.title ?? c.pageId;
    case "graph":
      return c.label ?? compactNodeLabel(c.nodeId);
  }
}
