> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo Speech — Capability-Library Guidance

`packages/mewbo_speech/src/mewbo_speech/` — text-to-speech and speech-to-text against the
same LiteLLM gateway the chat models come from. Imports **down** into `mewbo_core` +
`pydantic` only; the network leg sits behind the `gateway` extra plus per-call import
guards.

| Module | What it owns |
|---|---|
| `audio.py` | `AudioContainer` — magic-byte identification and the MIME label derived from it |
| `models.py` | `SpeechModel`, `SpeechMode`, and the closed voice/format vocabularies |
| `operations.py` | The request/result discriminated unions; each variant owns its validators, SDK kwargs and response parsing |
| `verbalize.py` | `MarkdownVerbalizer` — markdown in, speech-ready text out. Pure; no gateway |
| `transport.py` | The `SpeechTransport` protocol and the one litellm/httpx implementation |
| `gateway.py` | `SpeechGateway` — the atomic client, transport injected |

## The gateway lies about three things, and each one is encoded here

Everything below was measured against the deployed proxy by real calls. None of it is
discoverable from the gateway's own metadata, which is exactly why it has to live in code.

**1. A model id is BARE on the wire, and PREFIXED in the SDK argument.** These are two
different strings and conflating them is the trap:

| Caller | Value | Result |
|---|---|---|
| raw REST `POST /v1/audio/speech` | `supertonic-3` | passes the key ACL |
| raw REST | `openai/supertonic-3` | **403 `key_model_access_denied`** |
| `litellm.aspeech(model=...)` | `supertonic-3` | **`BadRequestError: LLM Provider NOT provided`**, before any socket opens |
| `litellm.aspeech(model=...)` | `openai/supertonic-3` | succeeds |

The SDK consumes the prefix as a LOCAL routing directive and strips it before writing the
body — captured on the wire, where the request reads `"model":"supertonic-3"` for a call
made with `openai/supertonic-3`. So `SpeechOperation.model` holds the bare id and REFUSES a
prefixed one, and `routed_model()` is the single place the prefix is applied.

**This is NOT core's `_resolve_litellm_model()` and must not reuse it.** For chat
completions the prefix also reaches the wire, so `llm.proxy_model_prefix` steers both legs
at once; here it must reach only the SDK. `DEFAULT_ROUTE_PREFIX` is deliberately a separate
constant.

**`model_info.key` is operator-facing bookkeeping, not a client-facing string.** It reads
`deepgram/nova-3` for the STT route, and `model="deepgram/nova-3"` makes litellm call
Deepgram's own native `/v1/listen` API directly, bypassing the proxy (404). Both modes take
the SAME form: `openai/<bare id>` in the SDK, bare on the wire.

**2. `Content-Type` describes nothing.** Every successful synthesis is served
`audio/mpeg`, and the payload is RIFF/WAVE by default or `fLaC` when FLAC is asked for —
never MPEG, not once. `AudioContainer.sniff` reads the payload's own leading bytes;
`SynthesisResult` keeps the declared header alongside the sniffed container so
`declared_type_was_wrong` stays observable instead of being silently corrected away.

**3. Every parameter failure is one opaque 500.** A missing voice, a bogus voice and an
unsupported `response_format` all return HTTP 500 with the literal body
`{"error":{"message":"Internal server error",...}}` — byte-identical across all of them
(confirmed by `md5sum`), naming no field. There is nothing to extract and nothing to
forward. **That is why the contracts refuse these before the call**: validation here is the
only diagnosis anyone will ever get.

- The eleven accepted voices are hardcoded in `SPEECH_VOICES` and there is no alternative:
  `/model/info` carries no voice metadata for either TTS model, and the error body
  enumerates nothing. The set was established by exhaustive trial, not introspection.
- `wav` (or omitted — byte-identical responses) and `flac` are the only working containers.
  `mp3`/`opus`/`aac`/`pcm` produce the same opaque 500.

## `mode` is the only capability signal, and core throws it away

`register_proxy_model_capabilities` (`mewbo_core/llm/llm.py`) fetches the same
`/model/info` document, defaults `mode` to `"chat"` and registers it with litellm — the
value itself never leaves that function. `LLMConfig.list_models()` returns bare id strings
from `/v1/models`, and the api's `GET /v1/models` adds only `supports_vision`. So nothing
downstream of core can distinguish a TTS route from an STT route from a chat route except
by a name heuristic.

`SpeechModel.from_model_info` is where that stops. Classification is ON the model: a new
mode is a new branch there, never a widening `if` in whichever caller was listing models.
A non-speech entry classifies to `None` rather than raising — a listing that raised on the
first chat model would report zero speech models on a healthy gateway.

## Performance

| Surface | Class |
|---|---|
| `AudioContainer.sniff`, `routed_model`, `litellm_kwargs`, `parse_response` | `O(1)` |
| `SpeechGateway.list_models` | `O(collection)` on a cache miss, `O(1)` on a hit |
| `SpeechGateway.run` | `O(input length)` |
| `MarkdownVerbalizer.verbalize` | `O(text length)` — ~0.5 ms/KB warm; ~7 ms once per process for mistune's lazy setup |

**Synthesis is not an interactive-latency call, and the FIRST one is far worse than the
rest.** Quote both numbers or a UI gets designed against the wrong one:

| | one sentence (~32 chars) | 410-char paragraph |
|---|---|---|
| **cold** — first call after a restart | **~4s to ~8s** | not separately measured |
| warm — every call after that | ~0.8-1.0s | ~4s |

`supertonic-3-hd` roughly doubles both and produces byte-identical output for the same
input, so "HD" buys nothing here but time.

The cold figure is a range because it was measured twice, on a contended box, at 3.7s (in
this package's live verification) and 7.9s (independently, by the api surface). Both are
first-call-in-a-fresh-process; neither is wrong. **A client showing a spinner should budget
~8s for the first press after a restart**, not the sub-second warm figure — that is the
number a user actually meets, once, and it is the one that decides whether the UI looks
broken.

The warm-only figure was in this file first, and it was the recon's. That is the same
warm-up trap the measurement note below records for `-hd`; it simply had not been applied
to the headline. Any surface calling this needs a pending state either way.

**`stream=true` is a no-op.** Time-to-first-byte equals total time with and without it
(3.63s vs 3.66s on the same paragraph) despite `Transfer-Encoding: chunked`: the backend
buffers the whole file before sending anything. Treat every call as await-then-play; there
is nothing to render progressively.

`list_models` caches for the life of the gateway instance, mirroring how core hydrates
proxy capabilities once per process per `api_base`. The document is ~120 KB and changes
only when an operator edits the proxy's routes — pass `refresh=True` after such an edit.

**`DEFAULT_TTS_MODEL` is `supertonic-3`, not `supertonic-3-hd`** (owner decision). The -hd
route measured ~2x slower for BYTE-IDENTICAL output — same voice, same format, same
16-bit mono 44.1 kHz, same file size, twice the wait. "HD" buys nothing here. It stays
selectable, never recommended.

## Live-verified, and the bug that only a live call could find

Both audio legs were driven through `SpeechGateway.run` against the deployed gateway.

| Leg | Result |
|---|---|
| `supertonic-3` synth | 215,084 B, `RIFF$H\x03\x00WAVEfmt `, sniffed WAV, declared `audio/mpeg` |
| `supertonic-3-hd` synth | byte-identical output, ~2.4x the latency |
| `nova-3` transcribe wav | 0.75s -> "testing muleba speech synthesis" |
| `nova-3` transcribe **webm/opus** | 0.21s -> real transcript |
| `nova-3` transcribe mp3 | 0.42s -> real transcript |

**`webm`/`opus` IS accepted** — what a browser `MediaRecorder` produces. No transcode
step is needed; do not build one. It is also the cheapest by an order of magnitude in
bytes (22 KB vs 215 KB for the same utterance).

**THE BUG THE OFFLINE SUITE COULD NOT SEE.** `run()` did not forward `api_base`/`api_key`
to the SDK at all — `litellm_kwargs()` returns only the operation's own arguments, and
nothing merged the gateway's coordinates in. Every live call failed with
`OpenAIException - Missing credentials`, naming an `OPENAI_API_KEY` nobody set and never
mentioning our proxy, because litellm silently fell back to its own provider resolution.

The suite was green the whole time, and worse than merely blind: the assertion spelled out
the credential-free kwargs dict as if it were correct, so **the test pinned the bug**. The
scripted double ignored what it was never sent. Two things now prevent a recurrence —
`api_base`/`api_key`/`timeout` are explicit keyword parameters on `SpeechTransport.invoke`
(a transport that forgets them cannot typecheck), and the double records them separately
so `test_the_gateway_coordinates_REACH_the_sdk_call` asserts they arrived.

Read that as the general rule: **an injected double proves the call SHAPE, never the
call's connection.** Anything resolved from config on the way out needs one real call.

**Latency: measure with a warm-up and alternate the models.** A naive single call per
model reported `-hd` as *faster* (1.89s vs 3.66s) — the first call was paying connection
setup. Warmed and alternated, n=4 each: `supertonic-3` median 0.80s, `supertonic-3-hd`
median 1.91s, output byte-identical at 215,084. Box was contended; treat as indicative.

## Chunking belongs to the client, and the package stays single-shot

`SynthesisRequest` bounds text at `min_length=1` and **no maximum**, deliberately. The
2000-character cap is `SynthesizeBody.MAX_TEXT_CHARS` in the api, published to clients as
`synthesis.limits.max_text_chars` — an app policy that is configurable and advertised, not
a gateway-intrinsic limit (nobody has measured the gateway's real text ceiling). Copying
the number down here would hardcode one surface's policy into a library every surface
shares, and the two would drift.

**Do not add a chunker to this package.** The two consumers that chunk are the console
(TypeScript) and Aura (Kotlin); a Python helper serves neither. Aura already has
`voice/SentenceChunker.kt`, and it is not a pure text split — it is *stateful and
stream-aware*, tracking a consumed offset in a growing assistant buffer so a sentence is
emitted exactly once even as the buffer is reconciled mid-stream. That state belongs to
playback sequencing, which is the client's job. A third implementation here would serve no
caller and would be one more thing to keep in agreement.

The package's contribution to chunked playback is the measured constraint below, not code.

## Verbalizing is NOT chunking, and the law above still stands

`MarkdownVerbalizer` turns markdown into speech-ready text. It does **not** split
text — the section above stays in force verbatim, and adding a chunker here is still
wrong for the reasons it gives. The two jobs separate cleanly on state: chunking is
stateful and stream-aware (Aura tracks a consumed offset into a growing buffer);
verbalizing is a pure function of one document, so a Python implementation serves
every caller of the API rather than none of them.

**It runs SERVER-SIDE inside `POST /api/speech/synthesize`, and that costs zero new
round trips** — both clients already POST every chunk to that endpoint. The request
field is `verbalize`, defaulted `true`; `false` speaks the string as written.
Advertised as `synthesis.verbalizes_markdown` so a client can retire its own
stripper instead of guessing. Neither client sends the field, so both get the
default and no client change was needed to land this.

**Until a client DOES retire its stripper, this runs second, and the composition is
safe but lossy.** Verified by bundling the console's real `stripSpeechMarkdown`
through its own esbuild and feeding the output here. Nothing mangles; what is lost
is what the client already destroyed before the parser could see it:

| | server over the ORIGINAL | server over the client's output |
|---|---|---|
| table | `Columns: Name, Cost.` / `a, 5.` | `Name, Cost.` / `a, 5.` — no announcement |
| ordered list | `1. First.` / `2. Second.` | `First Second.` — numbers gone, items merged |

That is the argument for the clients dropping their strippers, and it is a
measurement rather than a preference. Retiring them is not in this change.

### The safe-direction argument is FALSE, and the ceiling is what replaces it

The tempting claim is that verbalized text is always shorter, so a chunk under a
client's limit stays under it. Measured over 1,262 real assistant replies:
**verbalization LENGTHENS 29.6% of them.** Median ratio 0.987, p90 1.045, largest
real growth 4 characters — small, but not zero, and "always shorter" is the kind of
claim a caller builds a bound on.

The expansion source is a code block, whose 3-character fence becomes a 19-character
sentence. Adversarial worst case — a document of nothing but empty fences — measured
**2.5x**. So `SynthesizeBody.MAX_VERBALIZED_CHARS` is `1.5 × MAX_TEXT_CHARS` and the
result is re-checked after verbalizing. **No corpus reply under the source cap
crosses even the source cap after verbalization**, so the ceiling refuses only the
adversarial shape. Its refusal names `verbalize=false`, never `max_text_chars`: the
caller respected the published cap, and telling them to shorten already-short text
sends them to fix nothing.

### Verbalized text was synthesized for real, and duration is NOT the win

Three corpus documents were verbalized and both forms synthesized against the
deployed engine. All produced real RIFF/WAVE audio:

| Document | raw markdown | verbalized | change |
|---|---|---|---|
| a reply with a fenced block | 9.84 s | **6.34 s** | −35.6% |
| a reply with an info-string fence | 23.12 s | 21.20 s | −8.3% |
| a reply with a 3-row table | 34.13 s | **34.52 s** | **+1.2%** |

**So do not sell this as a duration saving.** Skipping a code block is a large real
win; a table costs slightly MORE, because `Columns: ` is added and the engine was
already collapsing the pipes to something short. The win is that the listener hears
column names and row values as prose instead of a pipe-delimited grid — a
correctness and comprehensibility claim, not a time one. Nobody has listened to
these clips; that is the check this cannot do for itself.

### The engine, measured — three facts that removed code

Each was measured by clip DURATION against the deployed engine, because its output
is not byte-deterministic (two identical requests differ byte-wise at the same
length), so only length is evidence.

| Probe | Result | Consequence |
|---|---|---|
| "Ruff linting passed." with 0, 2 and 13 emoji | **141,356 B / 1.60 s, all three** | The engine strips emoji itself. No emoji handler here. |
| `A.\nB.` vs `A. B.` vs `A.\n\nB.` | 2.090 s, 2.090 s, **2.808 s** | Only a BLANK line pauses. A single newline is a space. |
| "hello" vs "hello." | identical | A trailing full stop is inaudible on the LAST unit; it earns its place only BETWEEN units. |
| "1. First step" vs "First step" | 1.741 s vs 1.324 s | The engine SPEAKS an ordinal — see below. |

**Ordered lists keep their numbers, and dropping them was a defect, not a
simplification.** Two independent findings: the ordinal is audible (above), and
output with numbers dropped is not stable under a second pass — a line beginning
`1. ` is markdown for an ordered list, so re-verbalizing silently renumbered or
removed items. That second one matters because both clients strip markdown before
posting, so this runs over already-processed text as often as not.

**Re-verbalizing preserves the WORDS of 94.2% of the corpus and pause structure of
less.** The 5.8% that change are documents whose PROSE contains literal
markdown-significant characters (`<input type="file">` quoted in a sentence, a
`*.md` glob); no parser can distinguish those from markup. Prefer passing the
original markdown once.

### Tables: `speak-header: once`, and a size gate was REJECTED on measurement

CSS 2 §17.7.1 makes `speak-header: once` the initial value — announce the columns
once, then read each row. Header-per-cell is the INTERACTIVE screen-reader
behaviour, where a listener arrows into a cell and must be told which column they
landed in; a linear read has no arrowing and pays 2-3x for a fact already stated.

A prior proposal was to summarise tables over 20 data cells or 4 columns. **Measured
against 116 real tables, that would have discarded the BODY of 51 of them (44%).**
Dropping content someone asked to hear is the worst failure available here — worse
than a long read, which is at least audible as a long read. Corpus shape: 3 columns
and 5 data rows at the median; 8 columns and 22 rows at the extremes.

### ⚠️ The PARSER drops a row, and `_repair_headerless_tables` is why it no longer does

GFM requires a `|---|` delimiter under a table's first row. Given a pipe block with
**no** delimiter, mistune's table plugin still parses a table — it promotes row 1 to
the header and then **discards row 2 outright**. Measured on blocks of 2, 3, 4 and 5
rows: the body comes back holding rows 3..N every time. Two rows in, one row out, no
exception and nothing in a log.

A whole reply almost never looks like this (1 of 1,262). **A CHUNK does** — a client
splitting a long answer mid-table sends a tail of bare pipe rows with the delimiter
left behind in the previous chunk, which is precisely the case an earlier note filed
as a harmless "loses its header re-attachment". It is not harmless: it silently loses
a ROW, the exact failure the size-gate rejection above is about.

The repair inserts the missing delimiter before parsing, because the row never
reaches the tree and so cannot be recovered in the walk. It fires only on a block
whose SECOND line is a pipe row that is not a delimiter, and then copies the rest of
the block through untouched. **That last clause is load-bearing** — an earlier cut
inserted a delimiter between every PAIR of rows, turning one table into a stack of
one-row tables each announcing its own header. A test pins the well-formed case for
that reason.

### What has NO handler, and why that is not an omission

Images, footnotes and LaTeX were **measured absent** — zero `![](...)`, zero `[^1]`,
and all 25 `$...$` matches were pairs of dollar amounts in prose. A handler for a
construct nothing emits is dead code that reads as tested. An image still degrades
correctly through the inline fallback (it reads alt text); that is a consequence of
the default, not a feature to rely on.

**No text-normalization stage, and none should be added.** The engine's own front end
was measured expanding `85%`, `Dr.`, `10:30` and `3rd` correctly, with an exact
duration match against a hand-expanded control for the percentage. It is weak on bare
long integers, `$3.50` and `-5C` — but it offers no switch to disable its front end,
so anything expanded here is processed twice. The off-the-shelf alternative measured
885 MB against this package's whole dependency set.

### `mistune` is a BASE dependency, and the extra's own test is why

An extra exists to keep something heavy or environment-bound off a bare install AND
to let the feature report itself absent. `mistune` fails both halves: 464 KB of pure
Python, zero runtime dependencies above 3.11 (`typing-extensions` below), BSD-3, no
system library, ~43 ms to import — and verbalization has no absent state worth
rendering, since the only fallback is the regex pass this exists to end. Behind an
extra it would need a guard at every call site whose fallback could only be that
regex. Contrast `litellm`/`httpx`, which are seconds to import, network-bound, and
genuinely optional. Same `>=3.0,<4.0` specifier the api already declares, so the
workspace resolves one version; `>=3.0` because the dict-AST shape this walks
(`renderer=None`, `table_head`/`table_body`, `codespan.raw`) is the v3 shape.

**One instance is shared and that is safe** — mistune allocates parse state per call,
verified by parsing 80 corpus documents across 8 threads and getting trees identical
to the single-threaded ones. The API builds one per controller, not per request.

### Sentence segmentation was evaluated and NOT adopted

`pysbd` is the right library if segmentation is ever needed: 516 KB, pure Python,
MIT, no model download, 8/11 on an English fixture set against the shipped regex's
6/11 and 11/11 with the correct language code, and it returns `char_span` offsets a
streaming chunker wants. Its costs are real and acceptable — dead upstream since
2021 (not archived), three `SyntaxWarning`s on import under 3.12, and ~12 ms on a
1,280-char paragraph against a synthesis that takes seconds. Depending on it would
beat vendoring: a dead-but-stable pure-Python library with no dependencies is low
risk, and vendoring means owning its bugs forever.

**It is not adopted because nothing here would call it.** Verbalization already emits
one unit per block, and those units are small: median 71 characters, p90 247, and
only 1.02% exceed the console's 600-character chunk target. Segmentation earns its
place inside a CHUNKER, and the chunkers are the clients'. Adding an unused
dependency to prove a survey happened is the wrong end of the trade. Revisit it the
day a chunker moves server-side — not before.

## Cancellation: free at the language level, expensive at the backend

Cancel the awaiting task — there is no cancel token and there should not be.
`asyncio.CancelledError` derives from `BaseException`, so it passes through the transport's
`except Exception` normalisation untouched. Measured: a ~4s synthesis cancelled at 0.30s
raised `CancelledError` at 0.31s. **Never widen that handler to `BaseException`** — a stop
would silently become a failed synthesis and the caller would carry on.

**The cancel does not reach the gateway's backend.** The abandoned synthesis keeps running
and holds one of the TTS backend's two `max_parallel_requests`:

| Scenario | Next short call |
|---|---|
| control, no cancellation | 1.09s |
| immediately after a cancel, same gateway | 14.89s |
| immediately after a cancel, **fresh gateway + fresh client** | 13.90s |
| after a cancel, having waited 6s | 8.53s |

A fresh client is equally slow, so the occupancy is **server-side** — not a poisoned local
connection pool, and nothing this package can fix. Recovery is also longer than the
abandoned paragraph's own ~4s of work; the magnitude is unexplained and stated as
unexplained rather than guessed at.

**The design consequence: an abandoned request costs roughly what it had left to do, so
short requests make barge-in cheap and long ones make it expensive.** Sentence-sized
chunks are therefore not only a payload-cap workaround — they are what stops a stop button
from parking a slot that every caller of the gateway shares, there being two.

## The concurrency gate is a bound, not a rate limiter — and why

**The gateway exposes no rate-limit signal.** It never answers `429`, carries no
`Retry-After` on a success, and six concurrent requests all returned `200` while
individually queueing between 1.35s and 5.61s — there is nothing here for a
rate limiter (`tenacity`, `aiolimiter`, `pyrate-limiter`, `limits`) to regulate,
because none of them meter against a signal the gateway sends. **Do not add
one.** What the measurements DO show is a shared backend with finite parallel
capacity (the two `max_parallel_requests` slots per TTS route named throughout
this file), so the correct instrument is a CONCURRENCY CAP — `SpeechGateway`
takes an injected `asyncio.Semaphore` (field `concurrency`, constructor kwarg
`max_concurrent_calls`, default `DEFAULT_MAX_CONCURRENT_CALLS = 4`) that
`run()` acquires for the whole transport call.

**The default is REASONED, not measured, and the file says so at the constant.**
Repeated interleaved trials comparing client bounds of 2 and 4 (the two-slot
figure and double it) produced overlapping medians under normal contention —
the gap between bounds was smaller than the trial-to-trial variance on a
shared box. A `bound=1` vs `bound=2` control DID separate cleanly, so
concurrency above 1 measurably helps; picking 4 over 2 is a judgement call
(headroom for the two-route split), not a confirmed optimum. Re-measure on a
quiet box before changing it either way, and do not present a future guess as
a measurement either.

**A caller who rebuilds the gateway per call must pass the SAME semaphore
every time, or the bound does nothing.** `SpeechGateway.from_config` is a
per-call construction on purpose (an operator re-pointing `speech.api_base`
via `PATCH /api/config` must take effect without a restart), and a fresh
default `Semaphore` on each call never accumulates state across calls — four
separate gateways each with their own fresh permit of 4 admit 16 concurrent
transport calls, not 4. `init_speech_routes` in the api builds ONE semaphore
at boot and threads it into every `from_config()` call through a `concurrency=`
closure; that wiring is the load-bearing half of this feature and is worth
re-reading if the bound ever appears to do nothing on a live deployment.

**Retries are asymmetric by direction, and the asymmetry is deliberate.**
`SpeechGateway.run` selects `synthesis_max_retries` or
`transcription_max_retries` from `request.REQUIRED_MODE` and passes it to
`SpeechTransport.invoke` as an explicit `max_retries` — never left to
litellm's own fallback (`litellm.num_retries or openai.DEFAULT_MAX_RETRIES`,
i.e. 3 attempts, sized for a cheap chat completion). Measured against a call
guaranteed to fail (a rejected `response_format`): 0.30s at 0 retries, 1.00s
at 1, 2.02s at 2 — each retry costs roughly the FULL request latency, with no
fast-fail path. Synthesis defaults to **1** retry: a failing synthesis burns
4-8s of cold-start time per attempt and an abandoned one parks a shared
backend slot for ~14s regardless of how it ends (see "Cancellation" above), so
a caller who exhausts retries has already paid occupancy cost the retries
cannot recover. Transcription defaults to litellm's own **2**: it is cheap
even doubled (~0.2-0.8s per attempt) and has no comparable slot-parking cost.

## Fan-out was considered for chunked playback and rejected — do not "optimise" this back in

Both clients (`speechChunks.ts`, `voice/SentenceChunker.kt`) synthesize
sentence-sized chunks SEQUENTIALLY with exactly one chunk of lookahead — chunk
N+1 synthesizes while chunk N plays, never chunk N+2. It is tempting to
"parallelize" this by firing every chunk's synthesis at once; the measurements
say not to.

**First-audio latency is bounded by chunk 1 alone, whichever strategy is
used.** Nothing can play before the first chunk finishes synthesizing, so
fanning out the REST of the chunks buys the user nothing they would notice —
the wait they experience is identical either way.

**Total wall time is also identical, because synthesis already outruns
playback by more than six to one.** A 600-character chunk (`TARGET_CHUNK_CHARS`
in `speechChunks.ts`) takes roughly 6s to synthesize against roughly 40s to
speak. Sequential-with-one-ahead already finishes synthesizing chunk N+1 with
enormous slack before chunk N stops playing — there is no queueing delay for
fan-out to remove, because none exists in the sequential design.

**What fan-out DOES change is pressure on a two-wide upstream.** The TTS
backend advertises `max_parallel_requests: 2` PER ROUTE, shared across every
caller of the gateway, not per user. A ten-chunk response fanned out at once
would try to occupy five times that capacity from a single click, 503 its own
later chunks via `SpeechRoutesController.MAX_CONCURRENT_CALLS`/`StreamCapacity`
at the api boundary, and degrade every OTHER concurrent caller of the same
gateway. The concurrency gate described above exists in part because a naive
client-side fan-out is exactly the failure mode it is sized to survive — but
surviving it is not the same as it being a good idea to cause.

**So: sequential-with-lookahead is correct, not merely adequate, and a change
proposing to parallelize chunk synthesis needs new evidence that first-audio
or total time actually improves — the measurements above say neither can.**

## `/model/info` is 403 for the runtime key — discovery is BLOCKED

`list_models()` cannot reach the gateway today:

```
{"detail": "Virtual key is not allowed to call this route.
 Only allowed to call routes: ['llm_api_routes'].
 Tried to call route: /v1/model/info"}
```

Identical from the host and from inside the api container, so it is the key's route
allowlist, not a network path. `/v1/models` still answers 200 for the same key but returns
bare ids with **no `mode`** — the one field that separates TTS from STT from chat — so it
cannot substitute.

The fix is operational: grant the key the route, or give discovery a separate admin key.
**Do not paper over it with a name heuristic.** Classifying `supertonic-3` as TTS because
of what it is called is exactly the guess this module exists to avoid; an operator naming
the models in config is the honest fallback.

**This also silently degrades core.** `register_proxy_model_capabilities` fetches the same
document and swallows any failure, so litellm's cost map is no longer hydrated from the
proxy — `supports_prompt_caching` and friends now answer from the bundled defaults for
every proxy-fronted model, product-wide, with nothing in a log anyone reads.

## `from_config()` inherits whole-document config validation

It raises if ANY part of `app.json` fails to validate — on this host,
`langfuse.host` referencing an unset `MEWBO_LANGFUSE_HOST` was enough to make
`SpeechGateway.from_config()` throw before it read a single speech field. That is
`get_config_value` -> `get_config()` -> `AppConfig.model_validate` behaving as designed and
shared by every consumer, so it is not patched around here. Worth knowing when speech
"cannot find its gateway" on a partially-configured host: the speech config is fine, the
document is not.

## "Optional" means both layers

`litellm` and `httpx` sit behind the `gateway` extra AND a guard at every call site.
`_require()` probes **per leg**: the model listing needs only httpx, a synthesis call only
litellm. Probing both on either would make a working listing depend on litellm's
seconds-long import and fail a leg for a dependency it never touches — the same reasoning
as the identity kernel's per-extra driver guard.

Absence is a state, not an error: `SpeechGateway.is_available()` answers it without
raising, and only an actual call raises `SpeechUnavailableError` naming the extra.

## Config

`SpeechGateway.from_config()` reads `speech.api_base`/`speech.api_key` and falls back to
`llm.*`, because the speech routes live on the same proxy. `get_config_value` walks a
missing field to its default rather than raising, so an absent `speech` section is
indistinguishable from an empty one — which is what lets this package work before a config
section exists. **Do not turn that into a hard requirement**: `AppConfig` is `extra="ignore"`,
so an unknown block in `app.json` is silently dropped, and a `speech` section only becomes
live when a typed field is added to `AppConfig` in core.

## Testing

Tests live under the root suite at `tests/speech/`, which `testpaths` already collects — a
`packages/mewbo_speech/tests/` directory would not be. The scripted transport swaps only
the socket; every request built and every response parsed is production code.

**One suite must execute the DEFAULT transport.** `TestDefaultTransportAgainstARealListener`
drives `LiteLlmSpeechTransport` against a loopback `HTTPServer`, because the URL join, the
Bearer header and the `data` unwrap exist only in the default implementation and an
injected-transport suite never runs them.

**Never null `sys.modules["litellm"]` to simulate absence.** Its submodules stay cached, so
the next real `import litellm` re-executes a half-populated package and dies with a
circular-import `AttributeError` — in a LATER test, which then fails for a reason unrelated
to what it asserts. Patch the transport module's own `importlib` reference in-process, or
use a fresh subprocess where nothing is cached yet.

## Pre-edit checklist

- [ ] Does new code import only `mewbo_core` + `pydantic` + `mistune` (down)?
- [ ] New heavy dependency: behind an extra AND guarded per leg at the call site?
- [ ] New verbalization rule: is the construct's corpus FREQUENCY measured, and is
      the engine's own behaviour for it measured by clip DURATION before writing a
      handler? Two rules were deleted this way (emoji, normalization).
- [ ] New request variant: does it own its validators, `SDK_OPERATION`, `litellm_kwargs`
      and `parse_response` — with no `if kind ==` added to `SpeechGateway`?
- [ ] New gateway rule learned from a real call: is the measurement written down next to
      the code that encodes it?
- [ ] New public method: does its docstring state a cost class?
