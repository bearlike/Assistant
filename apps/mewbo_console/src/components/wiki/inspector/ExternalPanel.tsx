/**
 * ExternalPanel — a resolved cross-file / import target (`External` AST node).
 *
 * Minimal by design: the name plus the sites that reference it (incoming
 * IMPORTS / REFERENCES / CALLS edges), each linkable.
 */

import type { GraphIndex, InspectorNode } from "./GraphIndex";
import { PanelShell } from "./PanelShell";
import { EdgeSection } from "./parts";

export function ExternalPanel({
  node,
  index,
  onNavigate,
}: {
  node: InspectorNode;
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
}) {
  const { id, label } = node.data;
  const { incoming } = index.edgesOf(id);

  return (
    <PanelShell kindLabel="External" nodeKind="External" title={label || id}>
      <EdgeSection
        title={`Referenced by (${incoming.length})`}
        edges={incoming}
        onNavigate={onNavigate}
      />
    </PanelShell>
  );
}
