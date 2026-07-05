/**
 * SymbolPanel — a code symbol: Class, Function, Method, or Interface.
 *
 * Name + `file#range`, the docstring (markdown), call relationships
 * (calls / called-by), extends/implements ties, and any anchored memory notes
 * (cross-layer `ANCHORS` edges from Memory nodes).
 */

import type { GraphIndex, InspectorNode } from "./GraphIndex";
import { PanelShell } from "./PanelShell";
import {
  DocstringMarkdown,
  EdgeSection,
  NodeLinkList,
  Section,
} from "./parts";

export function SymbolPanel({
  node,
  index,
  onNavigate,
}: {
  node: InspectorNode;
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
}) {
  const { id, label, kind, file, range, docstring, layer } = node.data;
  const { incoming, outgoing } = index.edgesOf(id);

  const calls = outgoing.filter((e) => e.edge.data.kind === "CALLS");
  const calledBy = incoming.filter((e) => e.edge.data.kind === "CALLS");
  const extendsOut = outgoing.filter((e) => e.edge.data.kind === "EXTENDS");
  const extendedBy = incoming.filter((e) => e.edge.data.kind === "EXTENDS");

  // Memory notes anchored to this symbol — incoming ANCHORS from Memory nodes.
  const anchoredMemory = incoming
    .filter((e) => e.edge.data.kind === "ANCHORS" && e.other?.data.kind === "Memory")
    .map((e) => e.other)
    .filter((n): n is InspectorNode => Boolean(n));

  return (
    <PanelShell kindLabel={kind} nodeKind={kind} layer={layer} title={label || id}>
      <Section title="Location" hidden={!file}>
        <code className="font-mono break-all">
          {file}
          {range ? ` · ${range[0]}–${range[1]}` : ""}
        </code>
      </Section>

      <Section title="Docstring" hidden={!docstring}>
        {docstring && <DocstringMarkdown text={docstring} />}
      </Section>

      <EdgeSection title={`Calls (${calls.length})`} edges={calls} onNavigate={onNavigate} />
      <EdgeSection
        title={`Called by (${calledBy.length})`}
        edges={calledBy}
        onNavigate={onNavigate}
      />
      <EdgeSection
        title={`Extends / implements (${extendsOut.length})`}
        edges={extendsOut}
        onNavigate={onNavigate}
      />
      <EdgeSection
        title={`Extended by (${extendedBy.length})`}
        edges={extendedBy}
        onNavigate={onNavigate}
      />

      <Section title="Anchored memory" hidden={anchoredMemory.length === 0}>
        <NodeLinkList nodes={anchoredMemory} onNavigate={onNavigate} />
      </Section>
    </PanelShell>
  );
}
