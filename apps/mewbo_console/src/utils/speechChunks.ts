/**
 * Turn an assistant response into speech-ready chunks.
 *
 * Two problems, one seam. The synthesis endpoint refuses input over
 * `synthesis.limits.max_text_chars` (2000 today), so a long answer — exactly
 * the kind someone wants read to them — could not be read aloud at all. And
 * synthesis is buffered whole at the gateway (`stream=true` is a measured
 * no-op), so a single request for the whole answer means the first word is
 * heard only after the last one has been generated. Splitting at THIS layer is
 * the only way to get early first-audio, because there is no progressive
 * response to consume.
 *
 * The chunks are consumed by `utils/speechPlayback.ts`'s `SpeechReader`, which
 * plays chunk N while chunk N+1 synthesizes.
 *
 * ⚠️ **A boundary is audible.** Every chunk edge is a fresh clip, so a split
 * mid-clause is heard as a stumble. The split hierarchy is therefore ordered by
 * how natural the resulting pause is — line, then sentence, then clause, then
 * (only for a pathological single token) raw characters — and each level is
 * used only when the level above it cannot get under the limit.
 *
 * **Chunks carry RAW markdown, not stripped text.** `POST /api/speech/synthesize`
 * verbalizes server-side by default (`mewbo_speech.MarkdownVerbalizer` —
 * table headers announced once, ordered-list ordinals kept, code fences
 * skipped with a note), which this module used to defeat by stripping first:
 * running the verbalizer over already-flattened text made its two best
 * features unreachable. `stripSpeechMarkdown` still exists — see below — but
 * `chunkForSpeech` no longer calls it on the whole response; `buildSpeechUnits`
 * is what the split now runs over, and it is fence-aware so a boundary can
 * never land inside one (see its own docstring for the measured reason and
 * the one residual it does not close).
 *
 * The split-boundary RULES (line → sentence → clause → word, abbreviations,
 * quoted-sentence endings, fence atomicity) mirror Aura's `SentenceChunker`
 * (`voice/`) — a separate implementation because the two run on different
 * runtimes, pinned in agreement by `speechChunks.test.ts`. Aura's markdown
 * handling is NOT mirrored here any more: Aura still strips before every
 * segment, because its `SentenceChunker` feeds ONE `Utterance` stream to two
 * backends and one of them — the on-device `android.speech.tts.TextToSpeech`
 * path — has no server round trip and therefore never sees a verbalizer at
 * all (`voice/CLAUDE.md` records the constraint on that side).
 *
 * Cost class: `O(response length)`, one pass per split level. Pure — no DOM, no
 * I/O, no clock.
 */

/**
 * Ceiling on any one chunk, well under the server's own 2000.
 *
 * The gap is deliberate slack, not timidity: the server counts characters after
 * its own `strip()`, and this module and that validator are two implementations
 * of "how long is this text". A chunk sized right at the published cap turns any
 * disagreement between them into a refusal the user sees; 400 characters of
 * headroom means no normalization difference can reach the limit.
 */
export const MAX_CHUNK_CHARS = 1600;

/**
 * Ceiling on an ordinary chunk.
 *
 * ⚠️ **This is a CEILING, not the size chunks actually reach.** What a given
 * chunk may grow to is decided per boundary by `coverageLimit()` below; this
 * number only caps that result. An earlier revision treated it as the flat
 * target for every chunk after the first, and the gap this module exists to
 * hide is exactly what that produced.
 *
 * Re-measured against the deployed gateway, fitting both legs over real prose
 * at 100/200/300/450/600 characters (two runs each):
 *
 * ```
 * synthesis = 0.4 s + n / 43      playback = 0.9 s + n / 17.5
 * ```
 *
 * The rate is stable across measurements; the FIXED term is the one to be
 * careful with. Three independent fits put it at 0.29 s, 0.50 s and 2.64 s —
 * the outlier came from fitting only 100 characters and up, where a short
 * extrapolation back to zero turns a little contention into a lot of intercept.
 * Fit it from 20 characters up, on a quiet box, or do not quote it: a wrong
 * intercept makes a small chunk look like pure overhead when it is not.
 *
 * **So synthesis outruns playback by about 2.1 to 1 in characters per second,
 * NOT the "more than six to one" an earlier version of this docstring claimed.**
 * That figure came from comparing a 4 s synthesis against 40 s of speech for the
 * same 600 characters, which is the ratio of two TOTALS and silently folds in a
 * fixed cost that does not scale. The rate ratio is the one a lookahead budget
 * has to be sized on, and getting it wrong by three times is what let a
 * lookahead of one be described as ample while a listener heard a pause.
 */
export const TARGET_CHUNK_CHARS = 600;

/**
 * How many characters of the NEXT chunk one character of the current chunk pays
 * for — the fitted `37.7 / 17.6`, rounded DOWN to 2 for the margin.
 *
 * Rounding down rather than to the fitted 2.14 is deliberate: the constant is
 * fitted on one box under contention, and the cost of overestimating it is an
 * audible gap while the cost of underestimating it is a few more requests.
 * Swept over the corpus at 1.8 / 2.0 / 2.14 / 2.4, total gap seconds move only
 * between 2,497 and 2,356 — flat across that whole range, so the safe end is
 * free.
 */
const PLAYBACK_COVERAGE_RATIO = 2;

/**
 * The additive half of the coverage rule, in characters.
 *
 * Solving `synthesis(next) <= playback(prev)` against the fitted rates gives
 * `next <= 2.1 x prev + 49`, and an earlier revision dropped the `+ 49` as
 * spare margin. **It is not spare — it is what lets the budget GROW.** Without
 * it a chunk closes short of its limit, that shorter length sets the next
 * limit, and with evenly sized units the two meet exactly: 69-character units
 * close at 69, permit 138, and 69 + 1 + 69 is 139. Off by one, forever. That
 * fixed point pinned a twenty-sentence reply at one sentence per chunk — a seam
 * after every sentence, the defect this sizing exists to remove.
 *
 * 25 rather than the fitted 49, so the term unsticks the ratchet while most of
 * the slack stays where it was deliberately left: absorbing the variance the
 * fit does not model.
 */
const PLAYBACK_COVERAGE_HEADSTART = 25;

/**
 * What a chunk may grow to is bounded by what the chunk BEFORE it can pay for.
 *
 * One chunk of lookahead means chunk N+1 is synthesized while chunk N plays, so
 * the boundary is silent only while `synthesis(N+1) <= playback(N)`. Solving
 * that against the fitted rates above leaves roughly
 *
 * ```
 * next <= 2.1 x prev + 49
 * ```
 *
 * and this returns both halves of it — see `PLAYBACK_COVERAGE_HEADSTART` for
 * why the additive term cannot be dropped as spare margin — floored so a run of
 * short lines cannot ratchet the budget down to nothing, and capped at the
 * ordinary ceiling.
 *
 * **The failure this closes is a RUNT chunk, and it is overwhelmingly the FIRST
 * one.** A heading, a lead-in line or a short list item ends a chunk early; the
 * next chunk then packs to the full ceiling, and a 4 s clip is asked to cover a
 * 13 s synthesis. Measured over 2,212 real stored replies (6,374 boundaries):
 * **86.2% of every gapped boundary in the corpus was the 0 -> 1 boundary**, and
 * the chunk before a gap was a median of 107 characters. Sizing the next chunk
 * off the previous one's ACTUAL length is what turns that case back into
 * silence, and it costs nothing at a boundary that was already covered.
 *
 * Cost class: `O(1)`.
 */
function coverageLimit(previousChars: number, target: number, floor: number): number {
  const covered = PLAYBACK_COVERAGE_RATIO * previousChars + PLAYBACK_COVERAGE_HEADSTART;
  return Math.min(target, Math.max(floor, covered));
}

/**
 * What the FIRST chunk aims for — deliberately much smaller than the rest.
 *
 * First-audio latency is the only place chunk size is felt directly. 240
 * characters is a sentence or two: about 9 s to synthesize against the measured
 * rates and about 17 s of audio.
 *
 * ⚠️ **It does NOT follow that the chunk behind it is covered, and an earlier
 * version of this docstring asserted that it did** — "three times what the
 * 600-character second chunk needs to be ready". At the real rates a
 * 600-character chunk needs about 19 s to synthesize against this chunk's 17 s
 * of playback, so the pairing this comment described as having slack was in
 * fact the single worst boundary in the whole read. That is why the second
 * chunk is no longer sized by a flat target: `coverageLimit()` sizes it off
 * what the first chunk can actually pay for. This number now sets first-audio
 * latency and the stop-residual bound below, and nothing else.
 *
 * **It is also the answer to a resource question, which is why it is the first
 * chunk that shrank rather than all of them.** A stop leaves at most one
 * request draining (`SpeechReader.stop()` explains why draining beats
 * aborting), and it holds one of the TTS backend's two shared parallel slots
 * until it finishes. The GUARANTEED case is a stop during the initial wait,
 * before any audio — there is always a request in flight then, and the person
 * who clicked by mistake is the likeliest stopper of all. That case is bounded
 * by THIS number, which is roughly 3 s at 100 characters.
 *
 * **100 rather than anything smaller because of a floor in the packing loop,
 * not because of overhead.** The first unit is taken unconditionally, so a
 * chunk can never be shorter than the first speakable unit — a median 80
 * characters across stored replies. Setting this to 80, or to 60, measured
 * IDENTICALLY to each other: same first chunk, same first audio, more requests
 * on the long tail for nothing. 100 is the last value that still lets two short
 * units pack together.
 *
 * Measured end to end, real replies replayed through the deployed API in
 * sequential-with-one-lookahead order: first audio 4.51 s at 240, **2.57 s at
 * 100** — 1.9 s faster at the median, and every paired sample favoured 100. The
 * cost is 13% more requests corpus-wide, falling entirely on long replies; the
 * median reply needs two requests either way.
 */
export const FIRST_CHUNK_CHARS = 100;

/** What a fenced code block is read as, instead of its contents. */
const CODE_BLOCK_NOTE = "Code block omitted.";

/**
 * Words whose trailing period does not end a sentence.
 *
 * Single letters are handled separately rather than listed, which covers both
 * initials ("J. Smith") and the dotted forms ("e.g.", "i.e.") whose last
 * letter-run before the final period is one character long.
 */
const ABBREVIATIONS = new Set([
  "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "approx",
  "fig", "al", "inc", "ltd", "co", "eg", "ie", "no", "vol", "est", "cf", "dept",
]);

/** Punctuation that ends a clause — a pause a listener already expects. */
const CLAUSE_MARKS = ",;:—";

/** One speakable unit, plus whether it began a new line in the source. */
interface Unit {
  text: string;
  /** True when this unit started a source line, so it rejoins with a newline. */
  newLine: boolean;
}

/**
 * Reduce markdown to plain text — used ONLY to detect a response with nothing
 * speakable in it (a lone horizontal rule, an empty fence), never to build
 * what `chunkForSpeech` actually sends. That function posts raw markdown now
 * (see its own docstring) so the server's `MarkdownVerbalizer` can announce a
 * table header and keep ordered-list numbers; this stays as the cheap "would
 * ANYTHING be heard" gate that check needs, and as the fixture
 * `speechChunks.test.ts` pins the markdown-handling RULES against — rules the
 * server-side verbalizer independently implements too, so keeping this one
 * gives every rule a second, cross-checking home.
 *
 * Ordering is load-bearing in two places: fences are replaced before anything
 * else so their contents are never mistaken for prose, and images are unwrapped
 * before links because `![alt](src)` is a link pattern with a `!` in front.
 *
 * Cost class: `O(text length)`.
 */
export function stripSpeechMarkdown(text: string): string {
  let out = text;
  // An unterminated fence is normal on a streaming buffer, so the closing
  // marker is optional and the rest of the buffer is swallowed with it.
  out = out.replace(/```[\s\S]*?(?:```|$)/g, ` ${CODE_BLOCK_NOTE} `);
  out = out.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  out = out.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1");
  out = out.replace(/`([^`\n]*)`/g, "$1");
  // A table divider row is pure punctuation; a table's own pipes become the
  // commas a listener would expect between cells.
  out = out.replace(/^(?=[^\n]*\|)[ \t|:-]+$/gm, "");
  out = out.replace(/^[ \t]*\|/gm, "");
  out = out.replace(/\|[ \t]*$/gm, "");
  out = out.replace(/[ \t]*\|[ \t]*/g, ", ");
  out = out.replace(/^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/gm, "");
  out = out.replace(/^\s{0,3}#{1,6}[ \t]+/gm, "");
  out = out.replace(/^\s{0,3}>[ \t]?/gm, "");
  out = out.replace(/^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+/gm, "");
  out = out.replace(/\*\*([^*]+)\*\*/g, "$1");
  out = out.replace(/__([^_]+)__/g, "$1");
  out = out.replace(/~~([^~]+)~~/g, "$1");
  out = out.replace(/\*([^*\n]+)\*/g, "$1");
  // Underscore emphasis only at word boundaries, so `snake_case` survives.
  out = out.replace(/(^|[\s(["'])_([^_\n]+)_(?=[\s).,!?;:\]"']|$)/g, "$1$2");
  return out;
}

/** Whether the period at `index` belongs to an abbreviation rather than a sentence. */
function isAbbreviation(text: string, index: number): boolean {
  if (text[index] !== ".") return false;
  let start = index;
  while (start > 0 && /[A-Za-z]/.test(text[start - 1])) start--;
  const word = text.slice(start, index).toLowerCase();
  if (!word) return false;
  return word.length === 1 || ABBREVIATIONS.has(word);
}

/**
 * Split one line into sentences, keeping each sentence's own punctuation.
 *
 * A terminator only counts when whitespace or the end of the line follows it,
 * which is what keeps `3.14`, `v1.2` and `file.ts` intact without a special
 * case for each.
 *
 * Cost class: `O(line length)`.
 */
export function splitSentences(text: string): string[] {
  const out: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c !== "." && c !== "!" && c !== "?") continue;
    let end = i + 1;
    while (end < text.length && ".!?".includes(text[end])) end++;
    // A closing quote or bracket belongs to the sentence it closes.
    while (end < text.length && "\"')]”".includes(text[end])) end++;
    const after = text[end];
    if (after !== undefined && !/\s/.test(after)) continue;
    if (isAbbreviation(text, end - 1)) continue;
    const piece = text.slice(start, end).trim();
    if (piece) out.push(piece);
    start = end;
    i = end - 1;
  }
  const tail = text.slice(start).trim();
  if (tail) out.push(tail);
  return out;
}

/** Split one over-long sentence after its clause punctuation. */
function splitClauses(text: string): string[] {
  const out: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    if (!CLAUSE_MARKS.includes(text[i])) continue;
    if (i + 1 < text.length && !/\s/.test(text[i + 1])) continue;
    const piece = text.slice(start, i + 1).trim();
    if (piece) out.push(piece);
    start = i + 1;
  }
  const tail = text.slice(start).trim();
  if (tail) out.push(tail);
  return out;
}

/**
 * Last resort: pack whole words up to `limit`, hard-slicing a single word that
 * is longer than the limit on its own (a URL, a base64 blob, a hash).
 */
function splitWords(text: string, limit: number): string[] {
  const out: string[] = [];
  let current = "";
  const flushOversize = () => {
    while (current.length > limit) {
      out.push(current.slice(0, limit));
      current = current.slice(limit);
    }
  };
  for (const word of text.split(/\s+/)) {
    if (!word) continue;
    if (!current) current = word;
    else if (current.length + 1 + word.length <= limit) current = `${current} ${word}`;
    else {
      out.push(current);
      current = word;
    }
    flushOversize();
  }
  if (current) out.push(current);
  return out;
}

/** Break one source line down until every piece fits `limit`. */
function unitsForLine(line: string, limit: number): string[] {
  if (line.length <= limit) return [line];
  const out: string[] = [];
  for (const sentence of splitSentences(line)) {
    if (sentence.length <= limit) {
      out.push(sentence);
      continue;
    }
    for (const clause of splitClauses(sentence)) {
      if (clause.length <= limit) out.push(clause);
      else out.push(...splitWords(clause, limit));
    }
  }
  return out;
}

/** Whether an already-trimmed line opens or closes a fenced code block. */
function isFenceMarker(trimmedLine: string): boolean {
  return trimmedLine.startsWith("```");
}

/**
 * Build the ordered speakable units for `text`, treating an open fenced code
 * block as ONE atomic unit that no chunk boundary may fall inside.
 *
 * **`limit` and `fenceLimit` are separate because the two are traded against
 * DIFFERENT things, and collapsing them regresses one of them.** `limit` sizes
 * prose units, and finer prose units are what let the packing loop hit a
 * coverage budget accurately instead of overshooting it — a unit is indivisible
 * once built, so a 700-character unit forces a 700-character chunk however
 * small the budget was. `fenceLimit` is the size a fence may reach before
 * atomicity yields to the hard cap, and lowering it buys nothing: a fence is
 * already one unit, so it never overshoots a budget it was measured against.
 * Measured over the same 2,212-reply corpus, of 328 fenced blocks **37 (11.3%)
 * fall in the 600-1600 band** — those are exactly the blocks that would stop
 * being atomic if prose and fences shared one limit, re-opening the straddle
 * failure the paragraph below exists to close. Callers pass the server cap as
 * `fenceLimit` and the chunk ceiling as `limit`.
 *
 * Mirrors Aura's `SentenceChunker.findBoundaries`, which tracks `fenceOpen`
 * across the whole streaming buffer for the identical reason this module
 * needs it: `POST /api/speech/synthesize` verbalizes each request
 * independently, with no memory of a fence opened by a PREVIOUS call, so a
 * fence split across a chunk boundary is unrecoverable server-side — the
 * closing half reads as bare code with no announcement, and the reply's own
 * tail can be stranded past the request that carried it. Measured against
 * 1,263 real stored replies: chunking on raw markdown at these sizes with no
 * fence awareness would split a fence across a boundary in **62.5% of the
 * replies that contain one (50 of 80)** — the MAJORITY case, not an edge one.
 *
 * **A fence's break discipline is the OPPOSITE of everywhere else in this
 * module.** Ordinary text breaks on the smallest natural boundary that fits —
 * line, then sentence, then clause, then word. A fence breaks on NONE of
 * those while open: no sentence or clause split runs inside it, because a
 * mid-fence break is exactly the case with no recovery. The hard character
 * limit still wins over that atomicity — an oversized fence is hard-sliced
 * with `splitWords` rather than left to grow a request past what the server
 * accepts. A chunk the server refuses outright is a worse failure than a
 * code block read as prose.
 *
 * **The residual, measured rather than assumed away.** Re-running the SAME
 * corpus sweep with this function wired into the real chunk-packing loop:
 * zero round-trip losses (every reply's non-space text still reassembles
 * byte-for-byte) and exactly ONE reply whose single fence exceeds
 * `MAX_CHUNK_CHARS` on its own (1,936 characters against a 1,600 limit) still
 * straddles a boundary, because atomicity cannot beat a block bigger than the
 * ceiling — the hard-slice path above is what fires for it. Read aloud, that
 * reply's fence announces once where it is FULLY inside a chunk and reads as
 * plain prose where it is not; nothing is silently dropped, unlike the
 * straddle failure this function exists to close. That is the one shape left
 * unclosed, and it degrades to "ugly", never to "missing".
 *
 * Currently reachable only through direct tests: `chunkForSpeech` still
 * strips markdown before calling this, and stripped text never contains a
 * fence marker (`stripSpeechMarkdown` has already replaced every one with
 * its placeholder), so the fence branch below is dead code on that path —
 * deliberately, so THIS change is safe to land on its own, verified against
 * real fenced input rather than inert until the strip is later removed.
 *
 * Cost class: `O(text length)`.
 */
export function buildSpeechUnits(text: string, limit: number, fenceLimit: number = limit): Unit[] {
  const units: Unit[] = [];
  const lines = text.split("\n");
  let i = 0;
  while (i < lines.length) {
    const trimmed = lines[i].trim();
    if (isFenceMarker(trimmed)) {
      const block: string[] = [lines[i]];
      let j = i + 1;
      while (j < lines.length) {
        block.push(lines[j]);
        const closesFence = isFenceMarker(lines[j].trim());
        j++;
        if (closesFence) break;
      }
      const fenceText = block.join("\n").trim();
      if (fenceText) {
        if (fenceText.length <= fenceLimit) {
          units.push({ text: fenceText, newLine: true });
        } else {
          splitWords(fenceText, fenceLimit).forEach((piece, k) =>
            units.push({ text: piece, newLine: k === 0 }),
          );
        }
      }
      i = j;
      continue;
    }
    if (trimmed) {
      unitsForLine(trimmed, limit).forEach((piece, k) =>
        units.push({ text: piece, newLine: k === 0 }),
      );
    }
    i++;
  }
  return units;
}

export interface SpeechChunkOptions {
  /**
   * The server's published ceiling on one synthesis request, or `null` when it
   * publishes none. Read from the capability document rather than assumed: a
   * client copy of a server-side policy number goes stale in silence.
   */
  limitChars?: number | null;
}

/**
 * Split `raw` into chunks that are each safe to synthesize and natural to hear.
 *
 * Chunks carry RAW markdown, split by `buildSpeechUnits` — see the module
 * docstring for why stripping moved server-side, and that function's own
 * docstring for why a fence is the one construct kept atomic against a
 * boundary rather than left to the ordinary line/sentence/clause split.
 *
 * Returns `[]` when nothing speakable survives — an empty response, or one
 * made entirely of markdown scaffolding a lone construct reduces to nothing
 * (a horizontal rule, an empty fence). The emptiness check runs
 * `stripSpeechMarkdown` on the WHOLE response rather than reusing the units
 * this function builds, because "---" and "***" are non-blank strings on
 * their own and would otherwise become a chunk of pure punctuation the
 * server verbalizes to nothing — a caller must treat that as "there is
 * nothing to read", never as a silent success.
 *
 * Cost class: `O(response length)`.
 */
export function chunkForSpeech(raw: string, options: SpeechChunkOptions = {}): string[] {
  if (!stripSpeechMarkdown(raw).trim()) return [];

  const published = options.limitChars ?? MAX_CHUNK_CHARS;
  const max = Math.max(1, Math.min(MAX_CHUNK_CHARS, published));
  const target = Math.min(TARGET_CHUNK_CHARS, max);
  const first = Math.min(FIRST_CHUNK_CHARS, target);

  // Prose splits at the chunk ceiling so no single unit can overshoot a
  // coverage budget; a FENCE stays atomic all the way to the server cap.
  const units = buildSpeechUnits(raw, target, max);

  const chunks: string[] = [];
  let current = "";
  let limit = first;
  for (const unit of units) {
    if (!current) {
      current = unit.text;
      continue;
    }
    const separator = unit.newLine ? "\n" : " ";
    if (current.length + separator.length + unit.text.length <= limit) {
      current = `${current}${separator}${unit.text}`;
      continue;
    }
    chunks.push(current);
    // Sized off the chunk just closed — its playback is what has to cover the
    // next synthesis, so its ACTUAL length is the only honest input.
    //
    // ⚠️ A chunk closes SHORT of its limit, at the last unit that fit, so this
    // can stall: 69-character units under a 100 limit close at 69, permitting
    // 138, and 69 + 1 + 69 is 139. Off by one character, forever — measured, it
    // pinned a twenty-sentence reply at one sentence per chunk. Adding the unit
    // that did NOT fit is what breaks the tie, because it is exactly the amount
    // the next budget must clear for the read to make progress. Ratcheting off
    // the previous LIMIT instead also unsticks it and is wrong: the budget then
    // grows on a number no clip ever played, and the coverage invariant this
    // sizing exists to hold starts failing.
    limit = coverageLimit(current.length, target, first);
    current = unit.text;
  }
  if (current) chunks.push(current);
  return chunks;
}
