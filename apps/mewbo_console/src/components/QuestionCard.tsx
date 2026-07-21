import { useMemo, useState } from 'react';
import {
  CheckCircle2,
  Loader2,
  MessageCircleQuestion,
  MinusCircle,
} from 'lucide-react';
import type {
  QuestionAnswerItemPayload,
  QuestionMeta,
  UserQuestionItem,
} from '../types';
import type { AnswerQuestionResult } from '../api/contracts';
import { Input } from './ui/input';

interface QuestionCardProps {
  question: QuestionMeta;
  /**
   * Submit the answers. Resolves to a structured outcome so the card can
   * settle silently when the question was resolved elsewhere (superseded /
   * terminated) versus surfacing a correctable message (invalid / forbidden).
   * Absent in read-only / shared views — the card then shows a waiting state.
   */
  onAnswer?: (
    callId: string,
    callToken: string,
    answers: QuestionAnswerItemPayload[],
  ) => Promise<AnswerQuestionResult>;
}

/** Per-question working state: selected option indexes XOR the "Other" text. */
type Draft = { selected: number[]; other: string };

/** Render the chosen answer for a settled question as display text. */
function renderChosen(q: UserQuestionItem, a: QuestionAnswerItemPayload): string {
  if (typeof a.text === 'string' && a.text.trim()) return a.text.trim();
  const idxs = a.selected_indexes ?? [];
  return idxs.map((i) => q.options[i]?.label ?? `#${i}`).join(', ') || '(no answer)';
}

/**
 * Inline ask-user-question card (native human-in-the-loop clarification),
 * rendered in ConversationTimeline between a user query and the assistant
 * response — the console analog of the plan-approval gate. While
 * `status === "pending"` it renders each question's options as radios
 * (single-select) or checkboxes (`multi_select`), plus an always-present
 * "Other" free-text input, and one Submit button disabled until every
 * question has a selection or text.
 *
 * After resolution the card settles in place: `answered` shows the chosen
 * labels/text and the surface that answered; `declined`/`interrupted`/
 * `cancelled` show a muted dismissed note. A 404/409 from Submit settles the
 * card as answered-elsewhere with no error residue (the matching
 * `user_question_answered` event flips the status authoritatively).
 */
export function QuestionCard({ question, onAnswer }: QuestionCardProps) {
  const { callId, callToken, questions, status, answers, answeredVia } = question;

  const [drafts, setDrafts] = useState<Draft[]>(() =>
    questions.map(() => ({ selected: [], other: '' })),
  );
  const [submitting, setSubmitting] = useState(false);
  // Set once the answer POST is accepted (or resolved elsewhere) — disables the
  // form while the authoritative `user_question_answered` event settles it.
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isPending = status === 'pending';

  const allAnswered = useMemo(
    () => drafts.every((d) => d.other.trim() !== '' || d.selected.length > 0),
    [drafts],
  );

  const setSelected = (qi: number, next: number[]) =>
    setDrafts((prev) =>
      // Selecting an option is XOR with free text — clear "Other" for this row.
      prev.map((d, i) => (i === qi ? { selected: next, other: '' } : d)),
    );

  const setOther = (qi: number, text: string) =>
    setDrafts((prev) =>
      // Typing free text clears any selected indexes (XOR per the contract).
      prev.map((d, i) => (i === qi ? { selected: text ? [] : d.selected, other: text } : d)),
    );

  const toggleCheckbox = (qi: number, oi: number) => {
    const cur = drafts[qi].selected;
    setSelected(qi, cur.includes(oi) ? cur.filter((x) => x !== oi) : [...cur, oi]);
  };

  const submit = async () => {
    if (!onAnswer || submitting || !allAnswered) return;
    const items: QuestionAnswerItemPayload[] = drafts.map((d) =>
      d.other.trim() ? { text: d.other.trim() } : { selected_indexes: d.selected },
    );
    setSubmitting(true);
    setError(null);
    try {
      const res = await onAnswer(callId, callToken, items);
      if (res.ok || res.kind === 'superseded' || res.kind === 'terminated') {
        // Accepted, or resolved elsewhere — settle silently and let the
        // `user_question_answered` event render the authoritative outcome.
        setSubmitted(true);
      } else {
        setError(res.message);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit answer.');
    } finally {
      setSubmitting(false);
    }
  };

  const title = questions.length > 1 ? `${questions.length} questions` : 'Question';

  const { Icon, iconClass, badgeClass, statusLabel } = settledChrome(status, submitted);

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3">
        <Icon className={`w-4 h-4 shrink-0 ${iconClass}`} />
        <span className="text-sm font-medium text-[hsl(var(--foreground))]">
          {title}
        </span>
        <span
          className={`text-2xs font-medium px-2 py-0.5 rounded-full border ${badgeClass}`}
        >
          {statusLabel}
        </span>
      </div>

      <div className="border-t border-[hsl(var(--border))] px-4 py-3 space-y-4">
        {questions.map((q, qi) => (
          <fieldset key={qi} className="min-w-0">
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-2xs font-medium px-2 py-0.5 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 text-[hsl(var(--muted-foreground))]">
                {q.header}
              </span>
            </div>
            <legend className="text-sm text-[hsl(var(--foreground))] mb-2">
              {q.question}
            </legend>

            {isPending && !submitted ? (
              <div className="space-y-1.5">
                {q.options.map((opt, oi) => {
                  const checked = drafts[qi]?.selected.includes(oi) ?? false;
                  return (
                    <label
                      key={oi}
                      className="flex items-start gap-2.5 px-2.5 py-2 rounded border border-[hsl(var(--border))] hover:bg-[hsl(var(--accent))]/40 transition-colors cursor-pointer"
                    >
                      <input
                        type={q.multi_select ? 'checkbox' : 'radio'}
                        name={`q-${callId}-${qi}`}
                        checked={checked}
                        disabled={submitting}
                        onChange={() =>
                          q.multi_select
                            ? toggleCheckbox(qi, oi)
                            : setSelected(qi, [oi])
                        }
                        className="mt-0.5 accent-[hsl(var(--primary))]"
                      />
                      <span className="min-w-0">
                        <span className="text-xs font-medium text-[hsl(var(--foreground))]">
                          {opt.label}
                        </span>
                        {opt.description && (
                          <span className="block text-2xs text-[hsl(var(--muted-foreground))] leading-snug">
                            {opt.description}
                          </span>
                        )}
                      </span>
                    </label>
                  );
                })}
                <Input
                  type="text"
                  value={drafts[qi]?.other ?? ''}
                  disabled={submitting}
                  onChange={(e) => setOther(qi, e.target.value)}
                  placeholder={q.options.length ? 'Other (type a different answer)…' : 'Type your answer…'}
                  aria-label={`Free-text answer for ${q.header}`}
                  className="mt-1 bg-[hsl(var(--background))]"
                />
              </div>
            ) : status === 'pending' ? null : (
              // status === 'pending' here means "submitted, awaiting the
              // authoritative user_question_answered event" — the footer shows
              // the spinner, so the row stays quiet rather than flashing a note.
              <SettledAnswer q={q} answer={answers?.[qi]} status={status} />
            )}
          </fieldset>
        ))}

        {isPending && !submitted && (
          <div className="flex flex-wrap items-center gap-3 pt-1">
            <button
              type="button"
              onClick={submit}
              disabled={!onAnswer || submitting || !allAnswered}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded border border-[hsl(var(--primary))]/40 bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary-text))] hover:bg-[hsl(var(--primary))]/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              Submit
            </button>
            {!onAnswer && (
              <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                Read-only view.
              </span>
            )}
            {error && (
              <span className="text-2xs text-[hsl(var(--warning))]" role="alert">
                {error}
              </span>
            )}
          </div>
        )}

        {isPending && submitted && (
          <div className="flex items-center gap-1.5 pt-1 text-2xs text-[hsl(var(--muted-foreground))]">
            <Loader2 className="w-3 h-3 animate-spin" />
            Submitting your answer…
          </div>
        )}

        {status === 'answered' && answeredVia && (
          <p className="text-2xs text-[hsl(var(--muted-foreground))]">
            answered via {answeredVia}
          </p>
        )}
      </div>
    </div>
  );
}

/** Muted resolved line for a single settled question. */
function SettledAnswer({
  q,
  answer,
  status,
}: {
  q: UserQuestionItem;
  answer?: QuestionAnswerItemPayload;
  status: QuestionMeta['status'];
}) {
  if (status === 'answered' && answer) {
    return (
      <p className="text-xs text-[hsl(var(--foreground))]">
        <span className="text-[hsl(var(--muted-foreground))]">Your answer: </span>
        {renderChosen(q, answer)}
      </p>
    );
  }
  const note =
    status === 'declined'
      ? 'Superseded by a new message.'
      : status === 'interrupted'
        ? 'Dismissed — the run was interrupted.'
        : status === 'cancelled'
          ? 'Dismissed — the session was cancelled.'
          : 'No answer recorded.';
  return <p className="text-xs text-[hsl(var(--muted-foreground))]">{note}</p>;
}

/** Header icon + badge styling for the card's status. */
function settledChrome(status: QuestionMeta['status'], submitted: boolean) {
  if (status === 'pending' && !submitted) {
    return {
      Icon: MessageCircleQuestion,
      iconClass: 'text-[hsl(var(--info))]',
      badgeClass: 'bg-[hsl(var(--info)/0.1)] text-[hsl(var(--info))] border-[hsl(var(--info)/0.3)]',
      statusLabel: 'Awaiting your answer',
    };
  }
  if (status === 'answered') {
    return {
      Icon: CheckCircle2,
      iconClass: 'text-[hsl(var(--success))]',
      badgeClass: 'bg-[hsl(var(--success)/0.1)] text-[hsl(var(--success))] border-[hsl(var(--success)/0.3)]',
      statusLabel: 'Answered',
    };
  }
  // pending-but-submitted, or declined/interrupted/cancelled.
  return {
    Icon: MinusCircle,
    iconClass: 'text-[hsl(var(--muted-foreground))]',
    badgeClass:
      'bg-[hsl(var(--muted))]/40 text-[hsl(var(--muted-foreground))] border-[hsl(var(--border))]',
    statusLabel: status === 'pending' ? 'Submitting' : 'Dismissed',
  };
}
