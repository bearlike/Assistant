/**
 * ModulePanel — a module / package node.
 *
 * A module groups files + symbols and participates in imports. Shows its path,
 * contained members (linkable), its imports / imported-by, and a docstring when
 * one is attached.
 */

import type { GraphIndex, InspectorNode } from "./GraphIndex";
import { PanelShell } from "./PanelShell";
import {
  DocstringMarkdown,
  EdgeSection,
  NodeLinkList,
  Section,
} from "./parts";

export function ModulePanel({
  node,
  index,
  onNavigate,
}: {
  node: InspectorNode;
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
}) {
  const { id, label, file, docstring } = node.data;
  const members = index.children(id);
  const { incoming, outgoing } = index.edgesOf(id);
  const imports = outgoing.filter((e) => e.edge.data.kind === "IMPORTS");
  const importedBy = incoming.filter((e) => e.edge.data.kind === "IMPORTS");

  return (
    <PanelShell kindLabel="Module" nodeKind="Module" title={label || id}>
      <Section title="Path" hidden={!file && !label}>
        <code className="font-mono break-all">{file || label}</code>
      </Section>

      <Section title={`Members (${members.length})`} hidden={members.length === 0}>
        <NodeLinkList nodes={members} onNavigate={onNavigate} limit={14} />
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

      <Section title="Docstring" hidden={!docstring}>
        {docstring && <DocstringMarkdown text={docstring} />}
      </Section>
    </PanelShell>
  );
}
