"""Static demo data for the Agentic Search mock API.

Ported from the design prototype's ``data.js``. This is in-memory seed data
that the store loads on import. When the real implementation lands, the
store swaps to a database-backed source and this file becomes obsolete.

Field names use snake_case across the wire (matching the rest of the API).
"""

# ruff: noqa: E501  -- keep demo dict literals readable on a single line

from __future__ import annotations

from copy import deepcopy

# -- MCP source catalog ----------------------------------------------------
# A "source" is one MCP-style connector. Static for the mock; in the real
# system this would be derived from the user's installed MCPs.

SOURCE_CATALOG: list[dict] = [
    {"id": "notion", "name": "Notion", "color": "#ffffff", "bg": "#191919", "glyph": "N", "desc": "Pages, wikis and databases."},
    {"id": "slack", "name": "Slack", "color": "#ffffff", "bg": "#611f69", "glyph": "#", "desc": "Channels, threads and DMs."},
    {"id": "drive", "name": "Google Drive", "color": "#ffffff", "bg": "#1a73e8", "glyph": "D", "desc": "Docs, sheets and slides."},
    {"id": "github", "name": "GitHub", "color": "#ffffff", "bg": "#1a1e22", "glyph": "G", "desc": "Issues, PRs and code."},
    {"id": "linear", "name": "Linear", "color": "#ffffff", "bg": "#5e6ad2", "glyph": "L", "desc": "Projects, issues and cycles."},
    {"id": "jira", "name": "Jira", "color": "#ffffff", "bg": "#2684ff", "glyph": "J", "desc": "Tickets and epics."},
    {"id": "gmail", "name": "Gmail", "color": "#ffffff", "bg": "#d14836", "glyph": "M", "desc": "Threads and attachments."},
    {"id": "web", "name": "Web search", "color": "#ffffff", "bg": "#C15F3C", "glyph": "W", "desc": "Public web via Brave + Exa."},
    {"id": "filesystem", "name": "Local filesystem", "color": "#ffffff", "bg": "#3a3a38", "glyph": "F", "desc": "Indexed repo / workspace files."},
    {"id": "figma", "name": "Figma", "color": "#ffffff", "bg": "#0acf83", "glyph": "F", "desc": "Designs, comments, components."},
]


# -- Demo workspaces -------------------------------------------------------

DEMO_WORKSPACES: list[dict] = [
    {
        "id": "eng-docs",
        "name": "Engineering docs",
        "desc": "Runbooks, RFCs and architecture across eng surfaces.",
        "sources": ["notion", "github", "drive", "linear", "filesystem"],
        "instructions": "Prefer RFCs and CLAUDE.md-style internal docs. De-dupe results pointing to the same feature.",
        "created": "Mar 12, 2026",
        "past_queries": [
            {"q": "permissioning model for sub-agents", "when": "2d ago", "results": 7},
            {"q": "hypervisor concurrency admission — why did we cap at 6?", "when": "6d ago", "results": 4},
            {"q": "RFC for virtual projects", "when": "Apr 09", "results": 9},
            {"q": "how we stream tool call spans to Langfuse", "when": "Apr 08", "results": 5},
        ],
    },
    {
        "id": "product",
        "name": "Product & launches",
        "desc": "PRDs, launch plans, customer feedback, GTM artifacts.",
        "sources": ["notion", "linear", "slack", "gmail", "drive"],
        "instructions": "Tie results to a launch milestone when possible. Surface customer quotes inline.",
        "created": "Feb 02, 2026",
        "past_queries": [
            {"q": "v0.0.10 launch checklist", "when": "1h ago", "results": 8},
            {"q": "customer feedback on the new plan-mode UX", "when": "4d ago", "results": 12},
        ],
    },
    {
        "id": "support",
        "name": "Support intel",
        "desc": "Customer tickets, Slack threads and public issues.",
        "sources": ["linear", "slack", "gmail", "github"],
        "instructions": "Cluster results by customer. Flag any ticket older than 14 days as stale.",
        "created": "Mar 28, 2026",
        "past_queries": [
            {"q": "open tickets about file-edit matcher", "when": "9m ago", "results": 6},
        ],
    },
    {
        "id": "research",
        "name": "Research library",
        "desc": "Papers, web reading list, internal notes.",
        "sources": ["web", "drive", "notion"],
        "instructions": "Cite primary sources. Include published date + author prominently.",
        "created": "Jan 17, 2026",
        "past_queries": [
            {"q": "parallel tool-use benchmarks 2026", "when": "3d ago", "results": 5},
        ],
    },
    {
        "id": "home-ops",
        "name": "Home-ops",
        "desc": "Personal infrastructure, DNS, Proxmox, HA configs.",
        "sources": ["github", "notion", "filesystem"],
        "instructions": "",
        "created": "Apr 03, 2026",
        "past_queries": [],
    },
    {
        "id": "design-system",
        "name": "Design system",
        "desc": "Tokens, components, design reviews.",
        "sources": ["figma", "notion", "github"],
        "instructions": "",
        "created": "Feb 21, 2026",
        "past_queries": [
            {"q": "rejected-plan color — why amber not red", "when": "Apr 11", "results": 3},
        ],
    },
]


# -- Canned run payload ----------------------------------------------------
# A single demo response that all queries return (matching prototype). The
# store filters ``results`` and ``trace`` by the workspace's enabled sources
# before returning so each workspace produces a coherent subset.

DEMO_ANSWER: dict = {
    "tldr": (
        "Sub-agents inherit a filtered allowlist from their parent at spawn — read scopes "
        "pass through, but shell and any write tool require explicit re-approval. The "
        "hypervisor enforces this at the admission boundary; denies are logged with both "
        "child and parent ids."
    ),
    "bullets": [
        {"text": "Source of truth is RFC-042 (supersedes RFC-028) — the spawn boundary strips shell + write tools.", "cites": ["r1"]},
        {"text": "Implemented in #412 against AgentHypervisor.spawn(); concurrent-safe tools batch, exclusive tools serialize.", "cites": ["r2"]},
        {"text": "Open issue MEE-218 — denies don’t yet include parent_id, audit trails ambiguous when multiple children run in parallel.", "cites": ["r6"]},
        {"text": "Aligns with OWASP AI-04 (least privilege for agent systems).", "cites": ["r8"]},
    ],
    "confidence": 0.86,
    "sources_count": 7,
}

DEMO_RESULTS: list[dict] = [
    {
        "id": "r1",
        "source": "notion",
        "kind": "docs",
        "finish_delay_ms": 1400,
        "relevance": 0.96,
        "title": "RFC-042 · Sub-agent permission scoping",
        "url": "notion.so/eng/rfc-042-subagent-permissions",
        "snippet": "The hypervisor admits child sessions under a concurrency budget and propagates a trimmed permission set — tools marked <mark>concurrent-safe</mark> are batched, exclusive tools serialize. The child inherits read scopes but must re-request any write or shell permission at spawn.",
        "author": "Krishna A.",
        "timestamp": "Updated Apr 14",
        "insight": {
            "label": "Agent insight",
            "body": "This RFC is the current source of truth — it supersedes RFC-028 (linked below). The RFC is referenced 11 times in the last 30 days across planning threads.",
        },
        "refs": [
            {"title": "RFC-028 · Initial permission model", "url": "notion.so/eng/rfc-028", "kind": "doc"},
            {"title": "permissions.py (core)", "url": "github.com/bearlike/Assistant/…/permissions.py", "kind": "code"},
        ],
    },
    {
        "id": "r2",
        "source": "github",
        "kind": "code",
        "finish_delay_ms": 2100,
        "relevance": 0.91,
        "title": "feat(hypervisor): scoped permissions per child session #412",
        "url": "github.com/bearlike/Assistant/pull/412",
        "snippet": "Adds <mark>permission</mark> scoping to <code>AgentHypervisor.spawn()</code>. Child sessions now inherit a filtered allowlist; shell and write tools require explicit re-approval. Tested against the concurrency admission path.",
        "author": "bearlike",
        "timestamp": "Merged Apr 12 · main",
        "refs": [
            {"title": "apps/core/hypervisor.py", "url": "github.com/.../hypervisor.py", "kind": "code"},
            {"title": "tests/test_permission_scoping.py", "url": "github.com/.../test_permission_scoping.py", "kind": "code"},
        ],
    },
    {
        "id": "r3",
        "source": "slack",
        "kind": "threads",
        "finish_delay_ms": 2600,
        "relevance": 0.82,
        "title": "#eng-core · thread on permission inheritance for spawn",
        "url": "slack.com/#eng-core/p1712845320",
        "snippet": 'dom: "if we pass the parent allowlist verbatim, the child can spawn its own grandchildren with the same permissions — do we want that?" · kri: "no, we strip <mark>shell</mark> and any *write* on spawn. child has to re-ask."',
        "author": "6 participants",
        "timestamp": "Apr 10 · 14:22",
    },
    {
        "id": "r4",
        "source": "figma",
        "kind": "design",
        "finish_delay_ms": 3100,
        "relevance": 0.78,
        "title": "Permission-prompt UI — sub-agent variant",
        "url": "figma.com/file/PeRm/permission-prompts",
        "snippet": "Explores the inline permission-request affordance shown in the tool log when a sub-agent needs to escalate. Uses the blue <code>--permission</code> token and the ChildArrow iconography.",
        "author": "design · krishna",
        "timestamp": "Updated Apr 08",
        "embed": {"kind": "figma", "title": "Permission prompts · 14 frames · 2 comments"},
    },
    {
        "id": "r5",
        "source": "notion",
        "kind": "docs",
        "finish_delay_ms": 3500,
        "relevance": 0.74,
        "title": "Runbook · How permissions propagate across parallel spans",
        "url": "notion.so/eng/runbooks/permissions-propagation",
        "snippet": "Step-by-step of what happens when the admit path denies a child request. Includes the <mark>permission</mark> prompt → allow/deny → retry loop, with Langfuse trace screenshots.",
        "author": "Krishna A.",
        "timestamp": "Updated Mar 30",
        "image": {"alt": "Permission flow diagram", "gradient": "linear-gradient(135deg, #1e3a8a 0%, #3b82f6 60%, #14b8a6 100%)"},
    },
    {
        "id": "r6",
        "source": "linear",
        "kind": "tickets",
        "finish_delay_ms": 4000,
        "relevance": 0.68,
        "title": "MEE-218 · Log every permission deny with child_id + parent_id",
        "url": "linear.app/mewbo/issue/MEE-218",
        "snippet": "Telemetry gap: permission denies on sub-agents don't include the parent session id, making audit trails ambiguous when a hypervisor is running multiple children in parallel.",
        "author": "Assigned — kri",
        "timestamp": "In progress · Cycle 14",
    },
    {
        "id": "r7",
        "source": "github",
        "kind": "tickets",
        "finish_delay_ms": 4400,
        "relevance": 0.61,
        "title": "issue #398 — sub-agent gets parent's shell perms after retry",
        "url": "github.com/bearlike/Assistant/issues/398",
        "snippet": "Reproducer: fork a session, deny the first shell prompt, allow the second. The child now silently keeps the permission across the admit boundary.",
        "author": "Reported by rpl",
        "timestamp": "Apr 03 · Closed by #412",
    },
    {
        "id": "r8",
        "source": "web",
        "kind": "web",
        "finish_delay_ms": 4700,
        "relevance": 0.55,
        "title": "Principle of Least Privilege for Agent Systems — OWASP AI Top 10",
        "url": "owasp.org/ai/agents/least-privilege",
        "snippet": "Generic framing: sub-agents inheriting parent permissions is identified as risk AI-04. Recommends explicit re-scoping at the spawn boundary.",
        "author": "owasp.org",
        "timestamp": "Updated 2026",
    },
    {
        "id": "r9",
        "source": "drive",
        "kind": "docs",
        "finish_delay_ms": 5200,
        "relevance": 0.48,
        "title": "Q1 security review — agent framework (slides)",
        "url": "docs.google.com/…/q1-sec-review",
        "snippet": "Section 3 covers the permission scoping redesign. Uses the permission-log glyphs (⛔ / ✓) as the visual language for the audit trail.",
        "author": "bearlike@",
        "timestamp": "Mar 20",
        "embed": {"kind": "slides", "title": "18 slides · last edited Mar 20"},
    },
]

DEMO_TRACE: list[dict] = [
    {
        "id": "a-notion",
        "agent_id": "4e93·notion",
        "name": "notion",
        "source_id": "notion",
        "slot": 1,
        "lines": [
            {"t_ms": 100, "glyph": "▶", "text": 'search_pages(q="permissioning model for sub-agents")'},
            {"t_ms": 400, "glyph": "·", "text": "scanned 842 pages · 18 candidates"},
            {"t_ms": 800, "glyph": "·", "text": "retrieve(rfc-042, runbooks/permissions…)"},
            {"t_ms": 1200, "glyph": "·", "text": "rank · dedupe · 2 results"},
            {"t_ms": 1400, "glyph": "✓", "text": "returned 2 results", "done": True},
        ],
    },
    {
        "id": "a-github",
        "agent_id": "7b1f·gh",
        "name": "github",
        "source_id": "github",
        "slot": 2,
        "lines": [
            {"t_ms": 100, "glyph": "▶", "text": "search_prs + search_issues"},
            {"t_ms": 600, "glyph": "·", "text": "14 PRs · 9 issues matched"},
            {"t_ms": 1400, "glyph": "·", "text": "read file hypervisor.py"},
            {"t_ms": 1800, "glyph": "·", "text": "diff+rank"},
            {"t_ms": 2100, "glyph": "✓", "text": "returned 2 results", "done": True},
        ],
    },
    {
        "id": "a-slack",
        "agent_id": "c012·slk",
        "name": "slack",
        "source_id": "slack",
        "slot": 4,
        "lines": [
            {"t_ms": 200, "glyph": "▶", "text": "search_messages(ch=#eng-core, #platform)"},
            {"t_ms": 800, "glyph": "·", "text": "41 messages · 6 threads"},
            {"t_ms": 1500, "glyph": "·", "text": "thread-rank · extract excerpt"},
            {"t_ms": 2600, "glyph": "✓", "text": "returned 1 result", "done": True},
        ],
    },
    {
        "id": "a-figma",
        "agent_id": "d29a·fg",
        "name": "figma",
        "source_id": "figma",
        "slot": 7,
        "lines": [
            {"t_ms": 300, "glyph": "▶", "text": "search_files(permission*)"},
            {"t_ms": 1100, "glyph": "·", "text": "3 files · 47 frames"},
            {"t_ms": 3100, "glyph": "✓", "text": "returned 1 result", "done": True},
        ],
    },
    {
        "id": "a-linear",
        "agent_id": "2e44·ln",
        "name": "linear",
        "source_id": "linear",
        "slot": 5,
        "lines": [
            {"t_ms": 200, "glyph": "▶", "text": "search_issues(label:permissions OR sub-agent)"},
            {"t_ms": 1100, "glyph": "·", "text": "7 issues matched · cycle 12-14"},
            {"t_ms": 4000, "glyph": "✓", "text": "returned 1 result", "done": True},
        ],
    },
    {
        "id": "a-web",
        "agent_id": "f801·web",
        "name": "web",
        "source_id": "web",
        "slot": 3,
        "lines": [
            {"t_ms": 200, "glyph": "▶", "text": "brave_search + exa (10 queries)"},
            {"t_ms": 1200, "glyph": "·", "text": "48 pages · read top 8"},
            {"t_ms": 3800, "glyph": "·", "text": "filter · cite · dedupe"},
            {"t_ms": 4700, "glyph": "✓", "text": "returned 1 result", "done": True},
        ],
    },
    {
        "id": "a-drive",
        "agent_id": "b812·drv",
        "name": "drive",
        "source_id": "drive",
        "slot": 6,
        "lines": [
            {"t_ms": 400, "glyph": "▶", "text": "search_files(mimeType=docs|slides)"},
            {"t_ms": 1800, "glyph": "·", "text": "23 docs · 6 slides"},
            {"t_ms": 5200, "glyph": "✓", "text": "returned 1 result", "done": True},
        ],
    },
    {
        "id": "a-fs",
        "agent_id": "5a2c·fs",
        "name": "filesystem",
        "source_id": "filesystem",
        "slot": 0,
        "lines": [
            {"t_ms": 200, "glyph": "▶", "text": 'ripgrep "permission" · 3 repos'},
            {"t_ms": 900, "glyph": "·", "text": "21 matches · 4 files"},
            {"t_ms": 2400, "glyph": "✓", "text": "no relevant hits", "empty": True, "done": True},
        ],
    },
]

DEMO_RELATED_QUESTIONS: list[str] = [
    "How does the hypervisor admit concurrent sub-agents?",
    "What permissions get stripped on spawn?",
    "Where are permission denies logged?",
    "Is plan-mode treated as a permission scope?",
    "Difference between RFC-028 and RFC-042?",
]

DEMO_RELATED_PEOPLE: list[dict] = [
    {"name": "Krishna A.", "role": "authored RFC-042", "initials": "KA", "color": 1},
    {"name": "bearlike", "role": "merged #412", "initials": "BL", "color": 2},
    {"name": "rpl", "role": "reported issue #398", "initials": "RP", "color": 4},
]

DEMO_TOTAL_MS = 5500


def fresh_workspaces() -> list[dict]:
    """Return a deep copy of the demo workspaces.

    The store mutates workspace state (past_queries, edits) so we hand out
    a deep copy to keep the seed pristine.
    """
    return deepcopy(DEMO_WORKSPACES)
