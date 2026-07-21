/**
 * FolderPanel — a folder grouping node (scene-synthesised `Folder` kind).
 *
 * Shows the folder path, child counts (subfolders / files / symbols beneath
 * it), its aggregated relationships to other folders, the top contained
 * symbols, and an "enter" affordance that navigates into the folder via
 * `onNavigate`.
 */

import { Button } from "@/components/ui/button";
import { FolderOpen } from "lucide-react";

import type { GraphIndex, InspectorNode } from "./GraphIndex";
import { PanelShell } from "./PanelShell";
import { EdgeSection, NodeLinkList, Section } from "./parts";
import { SYMBOL_KINDS } from "./palette";

export function FolderPanel({
  node,
  index,
  onNavigate,
}: {
  node: InspectorNode;
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
}) {
  const { id, label, folderPath } = node.data;
  const children = index.children(id);

  const subfolders = children.filter((c) => c.data.kind === "Folder");
  const files = children.filter((c) => c.data.kind === "File");
  const symbols = collectSymbols(children, index);

  const { incoming, outgoing } = index.edgesOf(id);

  return (
    <PanelShell kindLabel="Folder" nodeKind="Folder" title={label || id}>
      <Section title="Path" hidden={!folderPath && !label}>
        <code className="font-mono break-all">{folderPath || label}</code>
      </Section>

      <Section title="Contents">
        <div className="flex flex-wrap gap-3 text-2xs text-[hsl(var(--muted-foreground))]">
          <Count n={subfolders.length} label="subfolders" />
          <Count n={files.length} label="files" />
          <Count n={symbols.length} label="symbols" />
        </div>
      </Section>

      {onNavigate && (
        <Button
          variant="neutral"
          size="sm"
          onClick={() => onNavigate(id)}
          className="w-full gap-1.5 h-7 text-2xs"
        >
          <FolderOpen className="h-3.5 w-3.5" />
          Enter folder
        </Button>
      )}

      <EdgeSection
        title={`Relationships out (${outgoing.length})`}
        edges={outgoing}
        onNavigate={onNavigate}
      />
      <EdgeSection
        title={`Relationships in (${incoming.length})`}
        edges={incoming}
        onNavigate={onNavigate}
      />

      <Section title="Top symbols" hidden={symbols.length === 0}>
        <NodeLinkList nodes={symbols} onNavigate={onNavigate} limit={10} />
      </Section>
    </PanelShell>
  );
}

function Count({ n, label }: { n: number; label: string }) {
  return (
    <span>
      <span className="text-[hsl(var(--foreground))]">{n}</span> {label}
    </span>
  );
}

/** Symbols directly under the folder's files (one level of containment). */
function collectSymbols(children: InspectorNode[], index: GraphIndex): InspectorNode[] {
  const out: InspectorNode[] = [];
  for (const child of children) {
    if (SYMBOL_KINDS.has(child.data.kind)) {
      out.push(child);
      continue;
    }
    if (child.data.kind === "File") {
      for (const sym of index.children(child.data.id)) {
        if (SYMBOL_KINDS.has(sym.data.kind)) out.push(sym);
      }
    }
  }
  return out;
}
