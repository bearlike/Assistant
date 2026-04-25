import { afterEach, expect, test } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CheckAgentsCard } from '../CheckAgentsCard';
import { AgentTreeNode } from '../../types';

afterEach(cleanup);

// ── Fixtures ported from the design's scenarios.js ──────────────────────

function makeAgent(o: Partial<AgentTreeNode> & { id: string; depth: number; task: string; status: string }): AgentTreeNode {
  return {
    id: o.id,
    parent_id: o.parent_id ?? null,
    depth: o.depth,
    task: o.task,
    status: o.status,
    steps_completed: o.steps_completed ?? 0,
    last_tool_id: o.last_tool_id ?? null,
    progress_note: o.progress_note ?? null,
    compaction_count: o.compaction_count ?? 0,
    result: o.result ?? null,
  };
}

const PARENT_ID_FANOUT = 'a1b2c3d4e5f6g7';
const FANOUT_AGENTS: AgentTreeNode[] = [
  makeAgent({ id: '41a0f2c9aabb00', parent_id: PARENT_ID_FANOUT, depth: 1, task: 'Map all auth middleware usages', status: 'completed', steps_completed: 12, result: { status: 'completed', summary: 'Found 23 call sites across 4 packages.', content: '' } }),
  makeAgent({ id: 'b8f210449c21de', parent_id: PARENT_ID_FANOUT, depth: 1, task: 'Rewrite middleware to async context pattern', status: 'running', steps_completed: 15, last_tool_id: 'apply_patch', progress_note: 'Rewrote middleware.py and 8 call sites; 15 to go.', compaction_count: 1 }),
  makeAgent({ id: 'c0117e2a33f400', parent_id: PARENT_ID_FANOUT, depth: 1, task: 'Review and add tests for new middleware', status: 'submitted' }),
];

const PARENT_ID_CANCEL = 'd1e2p3l4o5y6aa';
const CANCEL_AGENTS: AgentTreeNode[] = [
  makeAgent({ id: 'mig0001aabbcc', parent_id: PARENT_ID_CANCEL, depth: 1, task: 'Run database migrations', status: 'completed', steps_completed: 8, result: { status: 'completed', summary: 'Migrations 0042–0047 applied.', content: '' } }),
  makeAgent({ id: 'dpl0002ddeeff', parent_id: PARENT_ID_CANCEL, depth: 1, task: 'Rolling deploy to prod fleet', status: 'cancelled', steps_completed: 42, result: { status: 'cancelled', summary: 'Cancelled by parent at step 42.', content: '' } }),
];

const RAW_FANOUT = `Agent tree:
Agents: 1 completed, 1 running, 1 submitted
- [41a0f2c9] completed: "Map all auth middleware usages" (12 steps -> success)
  - [b8f210449] running: "Rewrite middleware to async context pattern" (15 steps, last: apply_patch | compacted x1 | progress: …)
  - [c0117e2a] submitted: "Review and add tests for new middleware" (0 steps)
2 agent(s) still running.`;

/**
 * The card is collapsed by default so the log view stays compact. Tests
 * that assert on body content must click the header first to expand it.
 */
async function renderExpanded(props: React.ComponentProps<typeof CheckAgentsCard>) {
  const user = userEvent.setup();
  const utils = render(<CheckAgentsCard {...props} />);
  await user.click(screen.getByText('check_agents'));
  return { user, ...utils };
}

// ── Tests ────────────────────────────────────────────────────────────────

test('header renders check_agents label and is collapsed by default', () => {
  render(
    <CheckAgentsCard
      agents={FANOUT_AGENTS}
      parentId={PARENT_ID_FANOUT}
      rawText={RAW_FANOUT}
    />,
  );
  expect(screen.getByText('check_agents')).toBeInTheDocument();
  // Body content is hidden until expanded.
  expect(screen.queryByText(/Map all auth middleware usages/)).toBeNull();
});

test('fanout — expanded card shows one row per agent', async () => {
  await renderExpanded({
    agents: FANOUT_AGENTS,
    parentId: PARENT_ID_FANOUT,
    rawText: RAW_FANOUT,
  });
  expect(screen.getByText(/Map all auth middleware usages/)).toBeInTheDocument();
  expect(screen.getByText(/Rewrite middleware to async context pattern/)).toBeInTheDocument();
  expect(screen.getByText(/Review and add tests for new middleware/)).toBeInTheDocument();
  expect(screen.getByText(PARENT_ID_FANOUT.slice(0, 8))).toBeInTheDocument();
});

test('fanout — running agent shows last_tool and compacted markers', async () => {
  await renderExpanded({
    agents: FANOUT_AGENTS,
    parentId: PARENT_ID_FANOUT,
    rawText: RAW_FANOUT,
  });
  expect(screen.getByText('apply_patch')).toBeInTheDocument();
  expect(screen.getByText(/compacted ×1/)).toBeInTheDocument();
});

test('fanout — completed agent shows RESULT line with summary', async () => {
  await renderExpanded({
    agents: FANOUT_AGENTS,
    parentId: PARENT_ID_FANOUT,
    rawText: RAW_FANOUT,
  });
  expect(screen.getByText(/RESULT\(completed\)/)).toBeInTheDocument();
  expect(screen.getByText(/Found 23 call sites/)).toBeInTheDocument();
});

test('fanout — running agent (no result) shows PROGRESS line with progress_note', async () => {
  await renderExpanded({
    agents: FANOUT_AGENTS,
    parentId: PARENT_ID_FANOUT,
    rawText: RAW_FANOUT,
  });
  expect(screen.getByText(/^PROGRESS$/)).toBeInTheDocument();
  expect(screen.getByText(/Rewrote middleware\.py and 8 call sites/)).toBeInTheDocument();
});

test('cancel scenario — shows cancelled status row with summary', async () => {
  await renderExpanded({
    agents: CANCEL_AGENTS,
    parentId: PARENT_ID_CANCEL,
    rawText: 'ignored for this test',
  });
  expect(screen.getByText(/Cancelled by parent at step 42/)).toBeInTheDocument();
  expect(screen.getAllByText('cancelled').length).toBeGreaterThan(0);
});

test('empty scenario — renders "No agents spawned." placeholder', async () => {
  await renderExpanded({
    agents: [],
    parentId: 'r0o0t000empty0',
    rawText: 'No agents.',
  });
  expect(screen.getByText('No agents spawned.')).toBeInTheDocument();
});

test('Raw tab shows the literal LLM-side text payload', async () => {
  const { user } = await renderExpanded({
    agents: FANOUT_AGENTS,
    parentId: PARENT_ID_FANOUT,
    rawText: RAW_FANOUT,
  });
  await user.click(screen.getByRole('tab', { name: /raw/i }));
  const panel = screen.getByRole('tabpanel');
  expect(within(panel).getByText(/Agents: 1 completed, 1 running, 1 submitted/)).toBeInTheDocument();
});

test('wait=true shows the wait badge (visible when collapsed)', () => {
  render(
    <CheckAgentsCard
      agents={FANOUT_AGENTS}
      parentId={PARENT_ID_FANOUT}
      rawText={RAW_FANOUT}
      wait
      waitedMs={14200}
    />,
  );
  expect(screen.getByText('wait=true')).toBeInTheDocument();
  expect(screen.getByText(/14\.2s wait/)).toBeInTheDocument();
});
