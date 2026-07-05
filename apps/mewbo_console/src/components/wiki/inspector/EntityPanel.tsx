/**
 * EntityPanel — an abstract-layer entity (concept / role / user-story / …).
 *
 * Shows the entity type, its classifier labels, the files/symbols it's anchored
 * to (cross-layer `ANCHORS` edges), and related entities (`RELATES` edges,
 * carrying an optional verb label).
 */

import type { GraphIndex, InspectorNode } from "./GraphIndex";
import { PanelShell } from "./PanelShell";
import {
  EdgeSection,
  LabelPills,
  NodeLinkList,
  Section,
} from "./parts";

export function EntityPanel({
  node,
  index,
  onNavigate,
}: {
  node: InspectorNode;
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
}) {
  const { id, label, entityType, labels } = node.data;
  const { incoming, outgoing } = index.edgesOf(id);

  // Anchored AST targets — outgoing ANCHORS from this entity to its code anchor.
  const anchors = outgoing
    .filter((e) => e.edge.data.kind === "ANCHORS")
    .map((e) => e.other)
    .filter((n): n is InspectorNode => Boolean(n));

  const relates = [...outgoing, ...incoming].filter(
    (e) => e.edge.data.kind === "RELATES",
  );

  return (
    <PanelShell kindLabel="Entity" nodeKind="Entity" layer="entity" title={label || id}>
      <Section title="Type" hidden={!entityType}>
        <code className="font-mono">{entityType}</code>
      </Section>

      <Section title="Labels" hidden={!labels || labels.length === 0}>
        {labels && labels.length > 0 && <LabelPills labels={labels} />}
      </Section>

      <Section title={`Anchored to (${anchors.length})`} hidden={anchors.length === 0}>
        <NodeLinkList nodes={anchors} onNavigate={onNavigate} />
      </Section>

      <EdgeSection
        title={`Related entities (${relates.length})`}
        edges={relates}
        onNavigate={onNavigate}
      />
    </PanelShell>
  );
}
