/**
 * The Q&A conversation orchestration extracted out of `QAScreen.tsx`: the
 * live-stream / persisted-snapshot join, the "which turn is current" ternary
 * ladder, the idempotent-URL fold effect, and the per-conversation reset
 * effect. `QAScreen` is left with rendering `renderedTurns` — every stateful
 * decision about WHAT to render lives here so it can be unit-tested without
 * mounting the two-column layout.
 *
 * See `CLAUDE.md` → "Idempotent Q&A URL" and "Multi-turn follow-up" for the
 * non-obvious rules this hook encodes (why `publishedAnswerRef` exists, why
 * `settled` masks the stream's folded state, why a follow-up doesn't navigate).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "wouter";

import { IndexedSnapshot } from "./indexedSnapshot";
import type { Citation } from "./citations";
import { useQaAnswerSnapshot, useWikiPage, useWikiProjectBySlug } from "./api/hooks";
import { useQaStream } from "./api/streamHooks";
import type { Block, QaTurn } from "./api/types";
import { buildHref } from "./router";
import { DEFAULT_WIKI_SLUG } from "./slug";
import { useStoredModel } from "./useStoredModel";

export interface QaConversationInput {
  question: string;
  pageId: string;
  slug?: string;
  model?: string;
  /** Persisted answer id (from `?answer=` in the URL). */
  answerId?: string;
}

/**
 * One fully-materialised turn ready to paint. Both a live/in-progress turn and
 * a completed (session- or snapshot-sourced) turn reduce to this shape so
 * `TurnView` renders them identically.
 */
export interface RenderedTurn {
  question: string;
  blocks: Block[];
  summarySources: string[] | null;
  done: boolean;
  errorMessage: string | null;
  accessedSources: string[];
  modelsUsed: string[];
  /** Authoring model for the "Generated with…" pill. */
  model: string;
}

/** Stable empty-block reference so a "no blocks yet" turn doesn't churn memos. */
const EMPTY_BLOCKS: Block[] = [];

export function useQaConversation({ question, pageId, slug, model: urlModel, answerId }: QaConversationInput) {
  const [, navigate] = useLocation();
  const [storedModel, setStoredModel] = useStoredModel();
  const repoSlug = slug ?? DEFAULT_WIKI_SLUG;
  const fromPageQuery = useWikiPage(pageId, repoSlug);

  // Repo snapshot (shared "wiki/projects" cache) → host-aware source URLs so
  // citation chips + file source cards open the cited file in the remote repo.
  const projectQuery = useWikiProjectBySlug(repoSlug);
  const repoSnapshot = useMemo(
    () => (projectQuery.data ? IndexedSnapshot.fromProject(projectQuery.data) : null),
    [projectQuery.data],
  );
  const resolveSourceHref = useCallback(
    (c: Citation) => repoSnapshot?.sourceUrl(c.path, c.startLine, c.endLine) ?? null,
    [repoSnapshot],
  );

  // Sync local picker to the URL model when present.
  useEffect(() => {
    if (urlModel && urlModel !== storedModel) {
      setStoredModel(urlModel);
    }
    // Only when urlModel arrives initially.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlModel]);

  const answeringModel = urlModel || storedModel;

  // ── Conversation state (multi-turn) ──────────────────────────────────────
  // Turns completed earlier in THIS browser session, frozen at the moment each
  // follow-up was asked (so they keep painting while the next turn streams).
  const [sessionTurns, setSessionTurns] = useState<RenderedTurn[]>([]);
  // Drives the live turn. `null` ⇒ pure-snapshot render (no POST). A follow-up
  // sets `{question, answerId}` with the SAME id so the backend continues the
  // session. The first cold turn seeds `{question}` (no id ⇒ mint a new answer).
  const [liveInput, setLiveInput] = useState<{ question: string; answerId?: string } | null>(
    answerId ? null : { question },
  );

  // Snapshot mode = no live turn AND the URL addresses a persisted answer.
  const isSnapshot = liveInput === null && Boolean(answerId);

  // Live QA stream — a no-op (`null`) in snapshot mode, so an idempotent
  // ``?answer=`` load never opens a stream.
  const stream = useQaStream(
    liveInput
      ? {
          question: liveInput.question,
          fromPageId: pageId,
          model: answeringModel,
          slug: repoSlug,
          answerId: liveInput.answerId,
        }
      : null,
  );

  // The stream's folded state still holds the PREVIOUS turn's blocks in the
  // window after swapping to a follow-up but before its first event lands;
  // `settled` masks that so the new turn paints a skeleton, not the old answer.
  const streamSettled = stream.settled;
  const streamBlocks = streamSettled ? stream.blocks : EMPTY_BLOCKS;
  const streamDone = streamSettled ? stream.done : false;
  const streamSummary = streamSettled ? stream.summarySources : null;
  const streamError = streamSettled ? stream.error : null;

  // The conversation's persisted id — the same across every follow-up.
  const activeAnswerId = answerId ?? stream.answerId;

  // Snapshot read: source of truth in idempotent mode; in stream mode it
  // backfills the deterministic provenance trail for the LATEST turn once the
  // stream settles (the stream's internal ``access`` events are ignored).
  const snapshot = useQaAnswerSnapshot(activeAnswerId, isSnapshot || streamDone);

  // ── The CURRENT (latest) turn — from the live stream or the snapshot top. ─
  const snapshotBlocks = snapshot.data?.blocks;
  const blocks = isSnapshot ? snapshotBlocks ?? EMPTY_BLOCKS : streamBlocks;
  const summarySources = isSnapshot
    ? snapshot.data?.summarySources ?? null
    : streamSummary;
  const done = isSnapshot ? snapshot.isSuccess || snapshot.isError : streamDone;
  const errorMessage = isSnapshot
    ? snapshot.isError
      ? "This answer is no longer available."
      : null
    : streamError?.message ?? null;
  // Provenance + per-probe model set describe the LATEST turn (snapshot top).
  const accessedSources = snapshot.data?.accessedSources ?? [];
  const modelsUsed = snapshot.data?.modelsUsed ?? [];
  const generatedWithModel = snapshot.data?.model || answeringModel;
  const currentQuestion = isSnapshot
    ? snapshot.data?.question ?? question
    : liveInput?.question ?? question;

  const currentTurn: RenderedTurn = {
    question: currentQuestion,
    blocks,
    summarySources,
    done,
    errorMessage,
    accessedSources,
    modelsUsed,
    model: generatedWithModel,
  };

  // Prior turns persisted for a shared-link snapshot (oldest first). Only used
  // in pure-snapshot mode; once a follow-up is asked this session, `sessionTurns`
  // (frozen at follow-up time) is the authoritative prior-turn record instead.
  const snapshotPriorTurns = useMemo<RenderedTurn[]>(
    () =>
      (snapshot.data?.turns ?? []).map((t: QaTurn) => ({
        question: t.question,
        blocks: t.blocks,
        summarySources: t.summarySources,
        done: t.status !== "running",
        errorMessage: t.status === "error" ? "This answer could not be generated." : null,
        accessedSources: t.accessedSources ?? [],
        modelsUsed: t.modelsUsed ?? [],
        model: t.modelsUsed?.[0] ?? snapshot.data?.model ?? answeringModel,
      })),
    [snapshot.data, answeringModel],
  );

  const priorTurns =
    sessionTurns.length > 0 ? sessionTurns : isSnapshot ? snapshotPriorTurns : [];
  const renderedTurns = [...priorTurns, currentTurn];

  // ``publishedAnswerRef`` marks a cold ask's freshly-minted id as OUR OWN, so
  // the fold below fires once and the conversation-identity below stays stable
  // across it. A foreign answer id (a shared link) is NOT ours.
  const publishedAnswerRef = useRef<string | null>(null);

  // Idempotent URL: once a COLD ask assigns an answer id (on `meta`), fold it
  // into the URL so a refresh re-reads the snapshot. Fires once per answer —
  // a follow-up reuses the same id, and a snapshot already carries it, so both
  // are gated out by the `answerId` guard.
  useEffect(() => {
    if (answerId) return;
    if (!stream.answerId) return;
    publishedAnswerRef.current = stream.answerId;
    navigate(
      buildHref({
        kind: "qa",
        question,
        pageId,
        slug,
        model: answeringModel,
        answer: stream.answerId,
      }),
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.answerId, answerId]);

  // Reset per-conversation state when a reused instance navigates to a DIFFERENT
  // conversation (a shared link to another answer, or a fresh cold question).
  // The self-fold — our own cold ask writing its minted id back into the URL —
  // keeps `question` and only adds our own id, so it reads as the same identity
  // and never wipes an in-progress conversation.
  const incomingConv =
    answerId && answerId !== publishedAnswerRef.current
      ? `answer:${answerId}`
      : `ask:${pageId}|${repoSlug}|${question}`;
  const handledConvRef = useRef(incomingConv);
  useEffect(() => {
    if (handledConvRef.current === incomingConv) return;
    handledConvRef.current = incomingConv;
    setSessionTurns([]);
    setLiveInput(answerId ? null : { question });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incomingConv]);

  const fromPageTitle = fromPageQuery.data?.title ?? pageId;

  // A follow-up stacks the current conversation and streams the next turn on
  // the SAME answer id (backend continues the session). Only once the active
  // turn has settled — mid-stream asks are ignored so a partial turn never
  // freezes into history. Deliberately NOT `useCallback`: it closes over
  // `currentTurn`/`renderedTurns`, which are freshly derived every render and
  // change more often than `activeAnswerId` — memoising against a narrower
  // dep list would capture a stale `renderedTurns` mid-stream.
  const onAsk = (q: string) => {
    const text = q.trim();
    if (!text || !currentTurn.done) return;
    setSessionTurns(renderedTurns);
    setLiveInput({ question: text, answerId: activeAnswerId ?? undefined });
  };

  return {
    repoSlug,
    fromPageTitle,
    repoSnapshot,
    resolveSourceHref,
    storedModel,
    setStoredModel,
    renderedTurns,
    onAsk,
  };
}
