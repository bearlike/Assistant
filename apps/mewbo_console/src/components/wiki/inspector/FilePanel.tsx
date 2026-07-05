/**
 * FilePanel — a source file node.
 *
 * Path + language, the symbols it contains (linkable), its imports /
 * imported-by, the module docstring (markdown), and — when the file maps to a
 * generated wiki page — a link into that page via the shared `buildHref`.
 */

import { FileText } from "lucide-react";

import type { GraphIndex, InspectorNode } from "./GraphIndex";
import { PanelShell } from "./PanelShell";
import { buildHref } from "../router";
import type { PlatformId } from "../router";
import {
  DocstringMarkdown,
  EdgeSection,
  NodeLinkList,
  Section,
} from "./parts";
import { SYMBOL_KINDS } from "./palette";

export function FilePanel({
  node,
  index,
  slug,
  platform,
  onNavigate,
}: {
  node: InspectorNode;
  index: GraphIndex;
  slug?: string;
  platform?: PlatformId;
  onNavigate?: (nodeId: string) => void;
}) {
  const { id, label, file, docstring } = node.data;
  // The scene may stamp a generated wiki page id onto file nodes; absent on
  // graph-only repos. Read defensively — no fetch, just a link when present.
  const pageId = (node.data as { pageId?: string }).pageId;
  const lang = (node.data as { lang?: string }).lang;

  const symbols = index.children(id).filter((c) => SYMBOL_KINDS.has(c.data.kind));
  const { incoming, outgoing } = index.edgesOf(id);
  const imports = outgoing.filter((e) => e.edge.data.kind === "IMPORTS");
  const importedBy = incoming.filter((e) => e.edge.data.kind === "IMPORTS");

  return (
    <PanelShell kindLabel="File" nodeKind="File" title={label || id}>
      <Section title="Path">
        <code className="font-mono break-all">{file || label}</code>
        {lang && (
          <div className="text-[hsl(var(--muted-foreground))] mt-1">
            language <span className="font-mono">{lang}</span>
          </div>
        )}
      </Section>

      {pageId && (
        <a
          href={buildHref({ kind: "page", pageId, slug, platform })}
          className="inline-flex items-center gap-1.5 text-[11px] text-[hsl(var(--primary))] hover:underline"
        >
          <FileText className="h-3.5 w-3.5" />
          Open wiki page
        </a>
      )}

      <Section title={`Symbols (${symbols.length})`} hidden={symbols.length === 0}>
        <NodeLinkList nodes={symbols} onNavigate={onNavigate} limit={14} />
      </Section>

      <EdgeSection
        title={`Imports (${imports.length})`}
        edges={imports}
        onNavigate={onNavigate}
      />
      <EdgeSection
        title={`Imported by (${importedBy.length})`}
        edges={importedBy}
        onNavigate={onNavigate}
      />

      <Section title="Module docstring" hidden={!docstring}>
        {docstring && <DocstringMarkdown text={docstring} />}
      </Section>
    </PanelShell>
  );
}
