/**
 * Frontmatter shape shared by wiki pages.
 *
 * The client-side frontmatter/TOC parser this file used to hold
 * (`parsePageSource`) is gone — `getPage()` (`api/client.ts`) now returns
 * `WikiPage` pre-parsed (frontmatter split, TOC derived) from the server, so
 * nothing here re-parses raw `.md` source anymore.
 */

import type { TocEntry } from "./types";

export interface PageFrontmatter {
  title: string;
  slug: string;
  relevantSources?: Array<{ path: string; lines?: string }>;
  sources?: Array<{ path: string; lines?: string }>;
  tocOverride?: TocEntry[];
}
