/**
 * MemoryPanel — a memory-orchestration unit.
 *
 * Shows the stored snippet (markdown), its classifier labels, and the AST/entity
 * anchors it's tied to (cross-layer `ANCHORS` edges). Provenance (entityType,
 * when the memory carries a kind/origin classifier) is surfaced when present.
 */

import type { GraphIndex, InspectorNode } from "./GraphIndex";
import { PanelShell } from "./PanelShell";
import {
  DocstringMarkdown,
  LabelPills,
  NodeLinkList,
  Section,
} from "./parts";

export function MemoryPanel({
  node,
  index,
  onNavigate,
}: {
  node: InspectorNode;
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
}) {
  const { id, label, snippet, labels, entityType } = node.data;
  const { incoming, outgoing } = index.edgesOf(id);

  // Everything this memory is anchored to (either direction of ANCHORS).
  const anchors = [...outgoing, ...incoming]
    .filter((e) => e.edge.data.kind === "ANCHORS")
    .map((e) => e.other)
    .filter((n): n is InspectorNode => Boolean(n));

  return (
    <PanelShell kindLabel="Memory" nodeKind="Memory" layer="memory" title={label || id}>
      <Section title="Kind" hidden={!entityType}>
        <code className="font-mono">{entityType}</code>
      </Section>

      <Section title="Content" hidden={!snippet}>
        {snippet && <DocstringMarkdown text={snippet} />}
      </Section>

      <Section title="Labels" hidden={!labels || labels.length === 0}>
        {labels && labels.length > 0 && <LabelPills labels={labels} />}
      </Section>

      <Section title={`Anchors (${anchors.length})`} hidden={anchors.length === 0}>
        <NodeLinkList nodes={anchors} onNavigate={onNavigate} />
      </Section>
    </PanelShell>
  );
}
