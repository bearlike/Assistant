import { afterEach, expect, test } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SpawnAgentCard } from '../SpawnAgentCard';

afterEach(cleanup);

const PR_RESEARCH_TASK =
  'Use the gh CLI and the Devin-AI-Wiki MCP tool to research GitHub PR #4287 from SimplifyJobs/mifflin.\n\n' +
  'Run this command to get PR details:\n```\ngh pr view 4287 --repo SimplifyJobs/mifflin\n```';

const PR_RESEARCH_PROPS = {
  caller: '09c085c1',
  childId: '4b1148c95827',
  task: PR_RESEARCH_TASK,
  allowedTools: ['aider_shell_tool', 'mcp___devin___ai_wiki_devin_ai_wiki_ask_question'],
  deniedTools: [],
  extras: [['root', '/home/kk/Projects/Mirrors/mifflin']] as const,
  message: 'Agent spawned. Use check_agents to monitor progress and collect results.',
  durationMs: 12,
};

async function renderExpanded(props: React.ComponentProps<typeof SpawnAgentCard>) {
  const user = userEvent.setup();
  const utils = render(<SpawnAgentCard {...props} />);
  await user.click(screen.getByText('spawn_agent'));
  return { user, ...utils };
}

test('header renders spawn_agent label and is collapsed by default', () => {
  render(<SpawnAgentCard {...PR_RESEARCH_PROPS} />);
  expect(screen.getByText('spawn_agent')).toBeInTheDocument();
  // The "task" FieldBlock label is only inside the expanded body — hidden until click.
  expect(screen.queryByText('task')).toBeNull();
});

test('branch line is always visible (caller, glyph, child label, preview) — no expand', () => {
  render(<SpawnAgentCard {...PR_RESEARCH_PROPS} />);
  expect(screen.getByText('09c085c1')).toBeInTheDocument();
  expect(screen.getByText('└─▸')).toBeInTheDocument();
  expect(screen.getByText('agent-4b1148c9')).toBeInTheDocument();
  // Truncated task preview (70 chars + …) shows in the branch line.
  expect(screen.getByText(/Use the gh CLI/)).toBeInTheDocument();
});

test('status pill defaults to "submitted" (static, no live join)', () => {
  render(<SpawnAgentCard {...PR_RESEARCH_PROPS} />);
  expect(screen.getByText('submitted')).toBeInTheDocument();
});

test('expanded body shows full task (Rendered tab) and acceptance criteria when present', async () => {
  await renderExpanded({
    ...PR_RESEARCH_PROPS,
    acceptance: 'pytest auth/ passes and ruff check shows no new issues.',
  });
  expect(screen.getByText('task')).toBeInTheDocument();
  expect(screen.getByText('acceptance_criteria')).toBeInTheDocument();
  expect(screen.getByText(/pytest auth\//)).toBeInTheDocument();
  // Full (un-truncated) task is in the body
  expect(screen.getByText(/gh pr view 4287/)).toBeInTheDocument();
});

test('agent_type pill renders in the header when provided', () => {
  render(<SpawnAgentCard {...PR_RESEARCH_PROPS} agentType="feature-dev:code-reviewer" />);
  expect(screen.getByText('feature-dev:code-reviewer')).toBeInTheDocument();
});

test('denied_tools render as red strikethrough chips', async () => {
  await renderExpanded({
    ...PR_RESEARCH_PROPS,
    deniedTools: ['shell_exec', 'browser_open'],
  });
  expect(screen.getByText('deny')).toBeInTheDocument();
  const denied = screen.getByText('shell_exec');
  expect(denied).toBeInTheDocument();
  expect(denied.className).toMatch(/line-through/);
});

test('scope shows "(all tools)" when neither allow nor deny is set', async () => {
  await renderExpanded({
    ...PR_RESEARCH_PROPS,
    allowedTools: [],
    deniedTools: [],
  });
  expect(screen.getByText('(all tools)')).toBeInTheDocument();
});

test('extras render as key/value pairs in the rendered tab', async () => {
  await renderExpanded(PR_RESEARCH_PROPS);
  expect(screen.getByText('extra')).toBeInTheDocument();
  expect(screen.getByText('root')).toBeInTheDocument();
  expect(screen.getByText('/home/kk/Projects/Mirrors/mifflin')).toBeInTheDocument();
});

test('Raw tab shows input and output JSON blocks', async () => {
  const { user } = await renderExpanded(PR_RESEARCH_PROPS);
  await user.click(screen.getByRole('tab', { name: /raw/i }));
  const panel = screen.getByRole('tabpanel');
  expect(within(panel).getByText('input')).toBeInTheDocument();
  expect(within(panel).getByText('output')).toBeInTheDocument();
  // Raw output includes the agent_id and submitted status verbatim.
  expect(within(panel).getByText(/4b1148c95827/)).toBeInTheDocument();
});
