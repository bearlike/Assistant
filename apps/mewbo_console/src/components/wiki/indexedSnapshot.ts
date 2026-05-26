/**
 * IndexedSnapshot — atomic class for "this wiki was generated from
 * branch/commit at a moment in time" metadata.
 *
 * Two render surfaces consume the same instance, today:
 *
 * - Wiki-page sidebar caption: ``formatSidebar()``
 *   → "INDEXED 4 MAY · MAIN · A1B2C3D" (uppercase, absolute date)
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

import { RelativeTime } from "./relativeTime";
import type { PlatformId } from "./router";
import type { Project } from "./api/types";

interface SnapshotPill {
  /** Visible label (uppercase or lowercase per formatter). */
  label: string;
  /** Optional href — null means render as plain text. */
  href: string | null;
  /** Optional aria/title hint (full SHA, branch name). */
  title?: string;
}

interface SnapshotRender {
  /** Date pill (always present). */
  date: SnapshotPill;
  /** Optional branch / commit / model pills, in render order. */
  extras: SnapshotPill[];
}

const ABSOLUTE_DATE_FMT =
  typeof Intl !== "undefined" && "DateTimeFormat" in Intl
    ? new Intl.DateTimeFormat("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
      })
    : null;

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

  /** Sidebar caption: uppercase tracking, absolute date. */
  formatSidebar(): SnapshotRender {
    return {
      date: {
        label: `INDEXED ${IndexedSnapshot._absoluteDate(this.indexedAt)}`,
        href: null,
        title: RelativeTime.tooltip(this.indexedAt),
      },
      extras: this._extras({ uppercase: true }),
    };
  }

  /** Landing-card footer: lowercase, relative date. */
  formatLandingCard(): SnapshotRender {
    return {
      date: {
        label: `Indexed ${RelativeTime.format(this.indexedAt)}`,
        href: null,
        title: RelativeTime.tooltip(this.indexedAt),
      },
      extras: this._extras({ uppercase: false }),
    };
  }

  // ── Private composition helpers ─────────────────────────────────────

  private _extras({ uppercase }: { uppercase: boolean }): SnapshotPill[] {
    const out: SnapshotPill[] = [];
    if (this.branch) {
      out.push({
        label: uppercase ? this.branch.toUpperCase() : this.branch,
        href: this._branchUrl(),
        title: `Branch: ${this.branch}`,
      });
    }
    if (this.commitShort) {
      out.push({
        label: uppercase ? this.commitShort.toUpperCase() : this.commitShort,
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

  private static _absoluteDate(iso: string): string {
    if (!iso || !ABSOLUTE_DATE_FMT) return iso ?? "";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return iso;
    return ABSOLUTE_DATE_FMT.format(new Date(t));
  }

  private static _stripTrailingSlash(url: string): string {
    return url.endsWith("/") ? url.slice(0, -1) : url;
  }
}

export type { SnapshotPill, SnapshotRender };
