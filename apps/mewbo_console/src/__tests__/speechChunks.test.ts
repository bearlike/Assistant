/**
 * The chunking rules, pinned as behaviour rather than as an implementation.
 *
 * What a listener actually notices is asserted here, in this order of
 * importance:
 *
 * 1. A response over the endpoint's cap is CHUNKED, never refused. This is the
 *    defect the feature exists to fix — the speaker button used to fail outright
 *    on exactly the long answers people most want read to them.
 * 2. No chunk can exceed the cap, whatever the input looks like. A single
 *    sentence, a single word or a single URL longer than the whole budget are
 *    all inputs a real transcript contains.
 * 3. Boundaries fall where a pause belongs. A chunk that ends mid-clause is
 *    heard as a stumble, so the split order (line → sentence → clause → word)
 *    is asserted level by level.
 * 4. `chunkForSpeech` sends RAW markdown, fences excepted. Everything else —
 *    a table header, an ordered list's numbers, emphasis markers — reaches
 *    the wire verbatim, because the server's `MarkdownVerbalizer` is what
 *    turns it into speech now. A fence is the one construct still handled
 *    client-side (via `buildSpeechUnits`' atomicity, tested separately below)
 *    because a fence split across two chunks is unrecoverable server-side.
 *    `stripSpeechMarkdown` still exists and is still tested at the bottom of
 *    this file, but it is no longer what `chunkForSpeech` sends — it is the
 *    "is there anything here at all" emptiness gate.
 */
import { describe, expect, it } from "vitest";
import {
  FIRST_CHUNK_CHARS,
  MAX_CHUNK_CHARS,
  TARGET_CHUNK_CHARS,
  buildSpeechUnits,
  chunkForSpeech,
  splitSentences,
  stripSpeechMarkdown,
} from "../utils/speechChunks";

/** The server's published ceiling today; the cap every chunk must clear. */
const SERVER_CAP = 2000;

/** `count` sentences of roughly `size` characters each, distinguishable by index. */
function sentences(count: number, size = 90): string {
  return Array.from({ length: count }, (_, i) => {
    const body = `Sentence number ${i} carries `.padEnd(size - 12, "x");
    return `${body.trim()} here.`;
  }).join(" ");
}

describe("chunkForSpeech — the size contract", () => {
  it("chunks a response past the endpoint's cap instead of refusing it", () => {
    // Ten paragraphs is the case the whole feature exists for: the endpoint
    // refuses input over 2000 characters, so this response could not be read
    // aloud at all before chunking.
    const long = Array.from({ length: 10 }, (_, i) => sentences(6).replace(/number \d+/g, `number ${i}`)).join("\n\n");
    expect(long.length).toBeGreaterThan(SERVER_CAP);

    const chunks = chunkForSpeech(long, { limitChars: SERVER_CAP });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) expect(chunk.length).toBeLessThanOrEqual(MAX_CHUNK_CHARS);
  });

  it("keeps every chunk under a LOWERED server cap, not under its own default", () => {
    // The ceiling is read off the capability document, so a deployment that
    // publishes a smaller number must actually narrow the chunks — a client
    // that only honoured its own constant would 400 on every request here.
    const chunks = chunkForSpeech(sentences(40), { limitChars: 300 });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) expect(chunk.length).toBeLessThanOrEqual(300);
  });

  it("starts small so the first audio arrives early, then settles into full-size chunks", () => {
    const chunks = chunkForSpeech(sentences(40), { limitChars: SERVER_CAP });

    expect(chunks.length).toBeGreaterThan(2);
    // The first chunk is the ONLY one whose size a listener feels: every later
    // chunk is synthesized while its predecessor plays.
    expect(chunks[0].length).toBeLessThanOrEqual(FIRST_CHUNK_CHARS);
    // And the rest grow — a uniformly tiny split would pass the assertion above
    // while adding an audible seam every two sentences. The budget RAMPS rather
    // than jumping, since each chunk is sized by what the one before it covers,
    // so the check is that it has grown by the time the read is under way.
    expect(chunks[chunks.length - 1].length).toBeGreaterThan(FIRST_CHUNK_CHARS);
    for (const chunk of chunks) expect(chunk.length).toBeLessThanOrEqual(TARGET_CHUNK_CHARS);
  });

  it("returns nothing for text with nothing speakable in it", () => {
    // Not an empty chunk and not a whitespace request the server would refuse:
    // the caller has to be able to tell "nothing to read" from "read this".
    expect(chunkForSpeech("")).toEqual([]);
    expect(chunkForSpeech("   \n\n  \t ")).toEqual([]);
    expect(chunkForSpeech("---\n\n***")).toEqual([]);
  });

  it("reads a short response as exactly one chunk", () => {
    // Chunking must not tax the common case with a second round trip.
    expect(chunkForSpeech("A sentence worth hearing.")).toEqual(["A sentence worth hearing."]);
  });
});

describe("chunkForSpeech — a chunk is sized by what the one before it can cover", () => {
  /**
   * The rates this rule is derived from, re-measured against the deployed
   * gateway and duplicated here ON PURPOSE.
   *
   * A test importing the constant it verifies proves only that the code equals
   * itself. These are written out independently so a change to the packing rule
   * has to be re-justified against the measurement rather than silently
   * re-baselined — the same reason the timeline corpus is authored rather than
   * dumped from either implementation.
   */
  const synthSeconds = (chars: number) => 0.4 + chars / 43;
  const playSeconds = (chars: number) => 0.9 + chars / 17.5;

  it("covers the heading-then-prose shape that produced the worst measured gap", () => {
    // THE regression, in the shape it actually occurs. A heading ends the first
    // chunk early; the old packer then filled the next one to the ceiling
    // regardless, so a 10-character clip preceded an 829-character synthesis —
    // a 20 s pause measured on this exact input. Measured over 2,212 real
    // replies, 86.2% of every gapped boundary in the corpus was this shape,
    // and the chunk before a gap was a median of 107 characters.
    const source = ["## A heading that ends the first chunk early", sentences(20)].join("\n\n");

    const chunks = chunkForSpeech(source, { limitChars: SERVER_CAP });

    expect(chunks.length).toBeGreaterThan(1);
    // Two independent halves of the cure, and the second is the one that
    // matters: the heading no longer STANDS ALONE (finer prose units let it
    // pack with the sentences behind it), and every boundary is covered.
    // Bounded by FIRST_CHUNK_CHARS now, so the claim is that the heading does
    // not stand ALONE — it packs with prose behind it — not that the chunk is
    // large. First audio is worth more than a tidy first boundary.
    expect(chunks[0].length).toBeGreaterThan(40);
    for (let i = 0; i < chunks.length - 1; i++) {
      expect(synthSeconds(chunks[i + 1].length)).toBeLessThanOrEqual(
        playSeconds(chunks[i].length),
      );
    }
  });

  it("keeps a full-size read's boundaries covered end to end", () => {
    // The property stated as a listener hears it: at no boundary may the next
    // chunk's synthesis outlast the current chunk's playback. Asserted across
    // EVERY boundary, because one uncovered seam is one audible pause.
    const chunks = chunkForSpeech(sentences(60), { limitChars: SERVER_CAP });

    expect(chunks.length).toBeGreaterThan(3);
    for (let i = 0; i < chunks.length - 1; i++) {
      expect(synthSeconds(chunks[i + 1].length)).toBeLessThanOrEqual(
        playSeconds(chunks[i].length),
      );
    }
  });

  it("still reaches full-size chunks once the read is under way", () => {
    // The counterweight: a rule that only ever shrinks chunks would trade the
    // gap for an audible seam every other sentence and 3x the requests against
    // a two-wide shared pool. The budget has to RECOVER.
    const chunks = chunkForSpeech(sentences(80), { limitChars: SERVER_CAP });

    expect(Math.max(...chunks.map((chunk) => chunk.length))).toBeGreaterThan(
      FIRST_CHUNK_CHARS * 2,
    );
  });

  it("loses no text to the finer prose units the rule needs", () => {
    // Splitting prose at the chunk ceiling rather than the server cap is what
    // lets the packer hit a coverage budget instead of overshooting it. It must
    // not cost a single character — a read that silently drops a sentence is a
    // worse defect than any gap.
    const source = sentences(40);

    const rejoined = chunkForSpeech(source, { limitChars: SERVER_CAP }).join(" ");

    expect(rejoined.replace(/\s+/g, "")).toBe(source.replace(/\s+/g, ""));
  });
});

describe("chunkForSpeech — where the boundaries fall", () => {
  it("breaks between sentences, never mid-clause", () => {
    const chunks = chunkForSpeech(sentences(40), { limitChars: SERVER_CAP });

    // Every chunk but the last ends on terminal punctuation. A chunk ending on
    // a word is a clip that stops mid-thought.
    for (const chunk of chunks.slice(0, -1)) {
      expect(chunk.trimEnd()).toMatch(/[.!?]["')\]]?$/);
    }
  });

  it("loses and duplicates nothing — the chunks reassemble into the source text", () => {
    // The assertion with the most power in this file. Every other rule here is
    // about WHERE a boundary falls; this one catches a boundary that ate a word
    // or repeated one, which is the failure a listener notices and no
    // length assertion can see. Compared against the RAW source now — not
    // `stripSpeechMarkdown(source)` — because that is what the chunks
    // actually carry; verbalization happens server-side, out of this
    // function's reach.
    const source = [
      "First paragraph with several sentences. It runs on for a while. Then it stops.",
      "- one bullet\n- another bullet",
      sentences(12),
      "A closing thought.",
    ].join("\n\n");

    const chunks = chunkForSpeech(source, { limitChars: SERVER_CAP });

    expect(chunks.length).toBeGreaterThan(1);
    const spoken = (text: string) => text.replace(/\s+/g, " ").trim();
    expect(spoken(chunks.join(" "))).toBe(spoken(source));
  });

  it("splits a single over-long sentence on its clause punctuation", () => {
    // No sentence boundary exists here at all, so the sentence level cannot
    // help and the clause level has to.
    const clause = "and then a further considerable quantity of qualifying detail arrives";
    const monster = `${Array.from({ length: 12 }, () => clause).join(", ")}.`;
    expect(monster.length).toBeGreaterThan(600);

    const chunks = chunkForSpeech(monster, { limitChars: 300 });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) expect(chunk.length).toBeLessThanOrEqual(300);
    // Each interior piece ends on the comma it was split after, so the pause
    // lands where the punctuation already asked for one.
    for (const chunk of chunks.slice(0, -1)) expect(chunk.trimEnd()).toMatch(/,$/);
  });

  it("hard-slices a single token longer than the whole budget rather than emitting it", () => {
    // A base64 blob or a very long URL has no boundary of any kind in it. The
    // only wrong answer is a chunk the server will refuse.
    const chunks = chunkForSpeech("x".repeat(900), { limitChars: 200 });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) expect(chunk.length).toBeLessThanOrEqual(200);
    expect(chunks.join("")).toBe("x".repeat(900));
  });

  it("keeps list items apart instead of running them into one another", () => {
    // Bullets carry no terminal punctuation, so joining them with a space would
    // read as "buy milk buy eggs". They rejoin on the newline they arrived on.
    // The `- ` markers themselves now reach the chunk verbatim — the server's
    // MarkdownVerbalizer is what turns each item into its own spoken unit, not
    // this function, which only decides where a POST boundary falls.
    const chunks = chunkForSpeech("Shopping:\n\n- buy milk\n- buy eggs\n- buy bread");

    expect(chunks).toHaveLength(1);
    expect(chunks[0]).toBe("Shopping:\n- buy milk\n- buy eggs\n- buy bread");
  });

  it("never straddles a code fence across two chunks, end to end through chunkForSpeech", () => {
    // The regression this whole change exists to prevent, exercised through
    // the PUBLIC function a caller actually uses — buildSpeechUnits' own
    // suite proves the unit builder is correct; this proves chunkForSpeech
    // is actually wired to it.
    const source = [
      sentences(3),
      "```python\ndef foo():\n    return 1\n```",
      "That should work as expected once you run it.",
    ].join("\n\n");

    const chunks = chunkForSpeech(source, { limitChars: 220 });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      const fenceCount = (chunk.match(/```/g) ?? []).length;
      expect(fenceCount % 2).toBe(0);
    }
    // The fence's own contents reach the wire raw (the server verbalizes
    // them), and the reply's closing sentence is never stranded.
    expect(chunks.some((c) => c.includes("def foo"))).toBe(true);
    expect(chunks.at(-1)).toContain("That should work as expected once you run it.");
  });
});

describe("buildSpeechUnits — fence atomicity, so a boundary can never straddle one", () => {
  // These call the unit builder DIRECTLY on raw markdown, bypassing the strip
  // that still runs inside chunkForSpeech today — the whole point of landing
  // this ahead of retiring the strip is proving it correct against real
  // fenced input before it is load-bearing.

  it("keeps a closed fence as one atomic unit, never split by sentence or clause", () => {
    const text = "Before.\n```python\ndef foo(): return 1\n```\nAfter.";
    const units = buildSpeechUnits(text, 1600);
    const texts = units.map((u) => u.text);

    expect(texts).toContain("```python\ndef foo(): return 1\n```");
  });

  it("swallows an unterminated fence to the end of the buffer, same as stripSpeechMarkdown", () => {
    const text = "Before.\n```python\ndef foo():\n    return 1";
    const units = buildSpeechUnits(text, 1600);

    expect(units.length).toBeGreaterThan(0);
    const lastText = units[units.length - 1].text;
    expect(lastText).toContain("```python");
    expect(lastText).toContain("return 1");
  });

  it("hard-slices a fence bigger than the whole chunk budget, rather than growing past it", () => {
    // The one residual this function does not close (documented in its own
    // docstring): atomicity cannot beat a single fence bigger than the
    // ceiling. The hard limit wins, same as splitWords does for a bare
    // oversized token.
    const text = "```\n" + "x".repeat(2000) + "\n```";
    const units = buildSpeechUnits(text, 200);

    expect(units.length).toBeGreaterThan(1);
    for (const u of units) expect(u.text.length).toBeLessThanOrEqual(200);
  });

  it("holds a fence atomic to the SERVER cap while prose splits at the chunk ceiling", () => {
    // The two limits are separate because they are traded against different
    // things: fine prose units let the packer hit a coverage budget, while a
    // fence gains nothing from being fine and loses atomicity. Measured over
    // the reply corpus, 37 of 328 fenced blocks (11.3%) sit in the 600-1600
    // band this split protects — collapsing the two limits re-opens the
    // straddle failure for every one of them.
    const fence = "```python\n" + "value = 1\n".repeat(60) + "```";
    expect(fence.length).toBeGreaterThan(TARGET_CHUNK_CHARS);
    expect(fence.length).toBeLessThan(MAX_CHUNK_CHARS);

    const units = buildSpeechUnits(`Before.\n\n${fence}\n\nAfter.`, TARGET_CHUNK_CHARS, MAX_CHUNK_CHARS);

    // Whole, despite being far past the limit prose is splitting at.
    expect(units.map((u) => u.text)).toContain(fence);
  });

  it("never drops or duplicates a character — round-trips through a fenced reply", () => {
    const source = [
      "First paragraph with several sentences. It runs on for a while.",
      "```js\nconsole.log('hi');\n```",
      "A closing thought after the code.",
    ].join("\n\n");

    const units = buildSpeechUnits(source, 2000);
    const rejoined = units
      .map((u, i) => (i === 0 ? u.text : (u.newLine ? "\n" : " ") + u.text))
      .join("");
    const norm = (s: string) => s.replace(/\s+/g, " ").trim();
    expect(norm(rejoined)).toBe(norm(source));
  });

  it("never leaves a chunk with an unbalanced fence-marker count, even at a tight limit", () => {
    // The actual regression: pack the units into MAX_CHUNK_CHARS-sized
    // chunks (the same packing loop chunkForSpeech uses) and confirm no
    // chunk's own ``` count is odd — an odd count is exactly what a
    // straddled fence produces. The surrounding text is padded so the
    // fence unit genuinely does not fit alongside its neighbours at this
    // limit, forcing a real boundary decision rather than one chunk holding
    // everything.
    const source = [
      sentences(3),
      "```python\ndef foo():\n    return 1\n```",
      sentences(3).replace(/number \d/g, "closing $&"),
    ].join("\n\n");

    const units = buildSpeechUnits(source, 220);
    const chunks: string[] = [];
    let current = "";
    for (const u of units) {
      if (!current) {
        current = u.text;
        continue;
      }
      const sep = u.newLine ? "\n" : " ";
      if (current.length + sep.length + u.text.length <= 220) {
        current = `${current}${sep}${u.text}`;
        continue;
      }
      chunks.push(current);
      current = u.text;
    }
    if (current) chunks.push(current);

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      const fenceCount = (chunk.match(/```/g) ?? []).length;
      expect(fenceCount % 2).toBe(0);
    }
  });
});

describe("splitSentences — the boundaries a naive split gets wrong", () => {
  it("does not break on an abbreviation, an initial, or a decimal", () => {
    expect(splitSentences("Dr. Smith read it. J. Doe agreed.")).toEqual([
      "Dr. Smith read it.",
      "J. Doe agreed.",
    ]);
    expect(splitSentences("It costs 3.14 per unit. That is fine.")).toEqual([
      "It costs 3.14 per unit.",
      "That is fine.",
    ]);
    expect(splitSentences("Open config.py and edit it. Then rerun.")).toEqual([
      "Open config.py and edit it.",
      "Then rerun.",
    ]);
    expect(splitSentences("Use a cache, e.g. Redis. It helps.")).toEqual([
      "Use a cache, e.g. Redis.",
      "It helps.",
    ]);
  });

  it("keeps a closing quote or bracket with the sentence it closes", () => {
    expect(splitSentences('He said "go now." Then he left.')).toEqual([
      'He said "go now."',
      "Then he left.",
    ]);
  });

  it("keeps a run of terminators together", () => {
    expect(splitSentences("Really?! I had no idea.")).toEqual(["Really?!", "I had no idea."]);
  });
});

describe("stripSpeechMarkdown — the emptiness gate, not the wire text", () => {
  it("replaces a fenced code block with a single spoken note", () => {
    const stripped = stripSpeechMarkdown("Try this:\n\n```py\nx = [1, 2]\nprint(x)\n```\n\nDone.");

    expect(stripped).toContain("Code block omitted.");
    expect(stripped).not.toContain("print");
    expect(stripped).not.toContain("```");
  });

  it("closes an unterminated fence at the end of the buffer", () => {
    // A streaming response is routinely mid-fence; reading the half-written
    // code aloud is worse than naming it.
    const stripped = stripSpeechMarkdown("Here:\n\n```py\nx = [1, 2]");

    expect(stripped).toContain("Code block omitted.");
    expect(stripped).not.toContain("x = [1, 2]");
  });

  it("speaks a link's text and never its URL", () => {
    expect(stripSpeechMarkdown("See [the guide](https://example.com/a/b?c=d).").trim()).toBe(
      "See the guide.",
    );
  });

  it("drops emphasis, headings, bullets and inline-code ticks", () => {
    const stripped = stripSpeechMarkdown("## Title\n\n- **bold** and *italic* and `code`");

    expect(stripped).toContain("Title");
    expect(stripped).toContain("bold and italic and code");
    expect(stripped).not.toMatch(/[*#`]/);
  });

  it("leaves an underscored identifier intact", () => {
    // Blanket underscore-stripping turns `max_text_chars` into `maxtextchars`,
    // which is neither the word nor the identifier.
    expect(stripSpeechMarkdown("Read `max_text_chars` from the document.")).toContain(
      "max_text_chars",
    );
  });
});
