/**
 * Citation grammar — the single parser/normalizer for the Q&A source
 * citation strings that flow through both the answer prose (inline ``src:``
 * links / chips) and the right-panel source cards.
 *
 * Grammar handled (DeepWiki-compatible):
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
    const range = c.startLine != null
      ? `#L${c.startLine}${c.endLine != null && c.endLine !== c.startLine ? `-${c.endLine}` : ""}`
      : "";
    return `src-${toDomToken(`${c.path}${range}`)}`;
  }

  /** Short ``path:line`` label for chips / card headers (no scheme prefix). */
  static label(c: Citation): string {
    if (c.scheme) return c.path;
    if (c.startLine == null) return c.path;
    const range = c.endLine != null && c.endLine !== c.startLine
      ? `${c.startLine}–${c.endLine}`
      : `${c.startLine}`;
    return `${c.path}:${range}`;
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
