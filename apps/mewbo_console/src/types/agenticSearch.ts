// Wire types for the Agentic Search API. Mirror the Python schema in
// `apps/mewbo_api/src/mewbo_api/agentic_search/`.

export interface SourceCatalogEntry {
  id: string
  name: string
  color: string
  bg: string
  glyph: string
  desc: string
}

export interface PastQuery {
  q: string
  when: string
  results: number
}

export interface Workspace {
  id: string
  name: string
  desc: string
  sources: string[]
  instructions: string
  created: string
  past_queries: PastQuery[]
}

export interface WorkspaceInput {
  name: string
  desc: string
  sources: string[]
  instructions: string
}

export interface ResultRef {
  title: string
  url: string
  kind: string
}

export interface ResultInsight {
  label: string
  body: string
}

export interface ResultImage {
  alt: string
  gradient: string
}

export interface ResultEmbed {
  kind: "figma" | "slides"
  title: string
}

export type ResultKind = "docs" | "code" | "threads" | "design" | "tickets" | "web"

export interface SearchResult {
  id: string
  source: string
  kind: ResultKind
  finish_delay_ms: number
  relevance: number
  title: string
  url: string
  snippet: string
  author: string
  timestamp: string
  insight?: ResultInsight | null
  refs?: ResultRef[]
  image?: ResultImage | null
  embed?: ResultEmbed | null
}

export interface TraceLine {
  t_ms: number
  glyph: string
  text: string
  done?: boolean
  empty?: boolean
}

export interface TraceAgent {
  id: string
  agent_id: string
  name: string
  source_id: string
  slot: number // 0..7 — maps to --agent-N tokens
  lines: TraceLine[]
}

export interface AnswerBullet {
  text: string
  cites: string[]
}

export interface RunAnswer {
  tldr: string
  bullets: AnswerBullet[]
  confidence: number
  sources_count: number
}

export interface RelatedPerson {
  name: string
  role: string
  initials: string
  color: number // 0..7 — maps to --agent-N
}

export interface RunPayload {
  run_id: string
  query: string
  workspace_id: string
  total_ms: number
  answer: RunAnswer
  results: SearchResult[]
  trace: TraceAgent[]
  related_questions: string[]
  related_people: RelatedPerson[]
}
