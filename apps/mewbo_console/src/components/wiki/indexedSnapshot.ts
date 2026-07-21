/**
 * IndexedSnapshot — atomic class for "this wiki was generated from
 * branch/commit at a moment in time" metadata.
 *
 * Two render surfaces consume the same instance, today:
 *
 * - Wiki-page sidebar caption: ``formatSidebar()``
 *   → "Indexed 2 hours ago · main@a1b2c3d" (relative date, branch+commit
 *     folded into ONE ref pill so the 260px rail reads as a single line)
 *
 * - Landing-card footer: ``formatLandingCard()``
 *   → "Indexed 2 hours ago · main · a1b2c3d" (lowercase, relative date)
 *
 * Missing values (legacy projects with no branch/commit) drop their pill
 * cleanly — the formatter never renders an empty pill or "·" separator
 * with nothing after it.
 *
 * Long-play extensions (model, commit author, commit date, …) belong on
 * this class. Add a private attribute, extend the formatters; nothing
 * else changes.
 */

import { RelativeTime } from "../../utils/relativeTime";
import type { PlatformId } from "./router";
import type { Project } from "./api/types";

interface SnapshotPill {
  /** Visible label. */
  label: string;
  /** Optional href — null means render as plain text. */
  href: string | null;
  /** Optional aria/title hint (full SHA, branch name). */
  title?: string;
  /** Optional leading glyph, rendered by the caption (ref pills only). */
  icon?: "branch";
}

interface SnapshotRender {
  /** Date pill (always present). */
  date: SnapshotPill;
  /** Optional branch / commit / model pills, in render order. */
  extras: SnapshotPill[];
}

export class IndexedSnapshot {
  readonly indexedAt: string;
  readonly branch: string | null;
  readonly commitSha: string | null;
  readonly commitShort: string | null;
  readonly maintainerEdited: boolean;
  readonly repoUrl: string | null;
  readonly source: PlatformId | null;

  private constructor(init: {
    indexedAt: string;
    branch: string | null;
    commitSha: string | null;
    commitShort: string | null;
    maintainerEdited: boolean;
    repoUrl: string | null;
    source: PlatformId | null;
  }) {
    this.indexedAt = init.indexedAt;
    this.branch = init.branch;
    this.commitSha = init.commitSha;
    this.commitShort = init.commitShort;
    this.maintainerEdited = init.maintainerEdited;
    this.repoUrl = init.repoUrl;
    this.source = init.source;
  }

  // ── Factories ───────────────────────────────────────────────────────

  /** Build a snapshot from a Project wire record. */
  static fromProject(p: Project): IndexedSnapshot {
    return new IndexedSnapshot({
      indexedAt: p.indexedAt,
      branch: p.branch ?? null,
      commitSha: p.commitSha ?? null,
      commitShort: p.commitShort ?? (p.commitSha ? p.commitSha.slice(0, 7) : null),
      maintainerEdited: Boolean(p.maintainerEdited),
      repoUrl: p.repoUrl ?? null,
      source: (p.source as PlatformId) ?? null,
    });
  }

  // ── Render shapes ───────────────────────────────────────────────────

  /** Landing-card footer: lowercase, relative date, separate branch/sha pills. */
  formatLandingCard(): SnapshotRender {
    return {
      date: {
        label: `Indexed ${RelativeTime.format(this.indexedAt)}`,
        href: null,
        title: RelativeTime.tooltip(this.indexedAt),
      },
      extras: this._extras(),
    };
  }

  /** Relative "Indexed 6 days ago" phrasing, shared by every surface. */
  indexedLabel(): string {
    return `Indexed ${RelativeTime.format(this.indexedAt)}`;
  }

  /** Absolute timestamp for the relative label's tooltip. */
  indexedTitle(): string {
    return RelativeTime.tooltip(this.indexedAt);
  }

  /**
   * Host-aware branch / commit URLs. Public because the snapshot card lays
   * the two out as separately-styled pills rather than one formatter-produced
   * row — the class owns URL knowledge, the component owns presentation.
   */
  branchUrl(): string | null {
    return this._branchUrl();
  }

  commitUrl(): string | null {
    return this._commitUrl();
  }

  /**
   * Host-aware "open this file in the source repo" URL for a cited path,
   * optionally deep-linked to a 1-based line range. Shares the same
   * ``repoUrl`` + ``source`` platform knowledge as {@link _branchUrl} /
   * {@link _commitUrl} so citation chips + source cards never re-derive it.
   *
   * **Pinned to the INDEXED COMMIT whenever we have one**, falling back to the
   * branch only for legacy records that carry no sha. A cited line range is
   * only truthful against the tree the wiki was generated from: point it at a
   * moving branch and ``#L63-76`` lands on whatever occupies those lines
   * today (this repo's ``main`` runs hundreds of commits past its snapshot),
   * or 404s outright once the file is renamed away.
   *
   *   github    → ``<repo>/blob/<ref>/<path>#L<a>-L<b>``
   *   gitea     → ``<repo>/src/commit/<sha>/<path>`` (branch form is
   *               ``/src/branch/<branch>/`` — the SEGMENT differs by ref kind,
   *               and ``/src/branch/<sha>`` 404s, so they can't be swapped)
   *   gitlab    → ``<repo>/-/blob/<ref>/<path>#L<a>-<b>``
   *   bitbucket → ``<repo>/src/<ref>/<path>#lines-<a>:<b>``
   *
   * Returns ``null`` when we can't build a faithful link — no repoUrl, no ref
   * of either kind, an empty path, or an azure/generic-git host with no
   * portable blob shape (we never mis-link rather than guess).
   */
  sourceUrl(path: string, startLine?: number | null, endLine?: number | null): string | null {
    if (!this.repoUrl || !path) return null;
    const sha = this.commitSha;
    if (!sha && !this.branch) return null;
    const base = IndexedSnapshot._stripTrailingSlash(this.repoUrl);
    const ref = sha ?? encodeURIComponent(this.branch as string);
    const encPath = path
      .replace(/^\/+/, "")
      .split("/")
      .map(encodeURIComponent)
      .join("/");
    const anchor = IndexedSnapshot._lineAnchor(this.source, startLine, endLine);
    switch (this.source) {
      case "github":
        return `${base}/blob/${ref}/${encPath}${anchor}`;
      case "gitea":
        return sha
          ? `${base}/src/commit/${sha}/${encPath}${anchor}`
          : `${base}/src/branch/${ref}/${encPath}${anchor}`;
      case "gitlab":
        return `${base}/-/blob/${ref}/${encPath}${anchor}`;
      case "bitbucket":
        return `${base}/src/${ref}/${encPath}${anchor}`;
      default:
        // azure / generic git — no portable blob shape; skip rather than mis-link.
        return null;
    }
  }

  // ── Private composition helpers ─────────────────────────────────────

  private _extras(): SnapshotPill[] {
    const out: SnapshotPill[] = [];
    if (this.branch) {
      out.push({
        label: this.branch,
        href: this._branchUrl(),
        title: `Branch: ${this.branch}`,
      });
    }
    if (this.commitShort) {
      out.push({
        label: this.commitShort,
        href: this._commitUrl(),
        title: this.commitSha ? `Commit: ${this.commitSha}` : undefined,
      });
    }
    return out;
  }

  private _branchUrl(): string | null {
    if (!this.repoUrl || !this.branch) return null;
    const base = IndexedSnapshot._stripTrailingSlash(this.repoUrl);
    // GitHub / GitLab / Gitea share /tree/<branch>; Bitbucket uses /src/<branch>;
    // Azure has no portable shape — skip rather than mis-link.
    if (this.source === "bitbucket") return `${base}/src/${encodeURIComponent(this.branch)}`;
    if (this.source === "azure" || this.source === "git") return null;
    return `${base}/tree/${encodeURIComponent(this.branch)}`;
  }

  private _commitUrl(): string | null {
    if (!this.repoUrl || !this.commitSha) return null;
    const base = IndexedSnapshot._stripTrailingSlash(this.repoUrl);
    if (this.source === "bitbucket") return `${base}/commits/${this.commitSha}`;
    if (this.source === "azure" || this.source === "git") return null;
    return `${base}/commit/${this.commitSha}`;
  }

  // ── Static utilities ────────────────────────────────────────────────

  private static _stripTrailingSlash(url: string): string {
    return url.endsWith("/") ? url.slice(0, -1) : url;
  }

  /** Host-specific ``#Lstart-Lend`` line-range anchor (empty when no start). */
  private static _lineAnchor(
    source: PlatformId | null,
    start?: number | null,
    end?: number | null,
  ): string {
    if (start == null) return "";
    const hi = end != null && end !== start ? end : null;
    switch (source) {
      case "bitbucket":
        return hi ? `#lines-${start}:${hi}` : `#lines-${start}`;
      case "gitlab":
        return hi ? `#L${start}-${hi}` : `#L${start}`;
      default:
        // github / gitea share the double-L range form.
        return hi ? `#L${start}-L${hi}` : `#L${start}`;
    }
  }
}

export type { SnapshotPill, SnapshotRender };
