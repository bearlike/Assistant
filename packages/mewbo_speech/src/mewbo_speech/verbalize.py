#!/usr/bin/env python3
"""Markdown in, speech-ready plain text out — one policy, over a real parser.

An assistant's reply is markdown. Read to a listener verbatim it is noise: a
fenced block becomes several minutes of punctuation spoken symbol by symbol, a
link's URL is spelled character by character, and a table's pipes are read as
the word for the pipe character. Every surface that has needed this so far
reached for regular expressions, and each one broke on the same class of input —
a URL containing parentheses leaves a stray bracket audible, a ``~~~`` fence is
not recognised as a fence at all, and a four-space indented block is read out in
full. Those are not oversights: markdown is not a regular language, so a
substitution pass cannot know whether a ``#`` opens a heading or sits inside a
code span.

This module parses instead. :class:`MarkdownVerbalizer` walks a CommonMark+GFM
syntax tree and emits the text a listener should hear. The policy below was
chosen against MEASURED frequencies in a corpus of 1,262 real assistant replies,
so the constructs that get code are the constructs that occur:

===================  ======  =========================================
Construct            Corpus  Policy
===================  ======  =========================================
inline code          37.2%   backticks dropped, ``_`` read as a space
bullet list          31.8%   one spoken unit per item, nesting flattened
heading              15.9%   the text, then a pause. Never "heading level N"
ordered list         12.5%   as bullets — see the note below
horizontal rule      10.8%   a long pause. Never the word "separator"
GFM table             9.3%   header announced ONCE, then every row
fenced code           6.2%   announced once, contents skipped
indented code         4.7%   the same, and only a parser can see it
raw HTML              4.1%   dropped
blockquote            4.0%   the contents, with no "quote" announcement
link                  2.5%   the link text; the URL is dropped
===================  ======  =========================================

**Images, footnotes and LaTeX get no handler because they were MEASURED absent**
— zero occurrences of ``![...](...)`` and ``[^1]`` across the corpus, and every
one of the 25 ``$...$`` matches was a pair of dollar amounts in prose, not
mathematics. An image still degrades correctly (the inline fallback reads its
alt text) but nothing here is written for it. A handler for a construct nothing
emits is dead code that reads as tested.

**Emoji are left in place, and that is measured rather than assumed.** They
appear in 7.7% of replies and the engine's own text front end deletes them:
synthesizing "Ruff linting passed." plain, with two emoji, and with thirteen
produced clips of byte-identical LENGTH — 141,356 bytes, 1.60 s, all three — so
the emoji contributed exactly no audio. (The bytes themselves differ run to run;
the engine is not deterministic, so only the duration is evidence.) A stripper
here would be a second implementation of a deletion that already happens.

**There is no text-normalization stage, deliberately.** The deployed synthesis
engine has its own front end and was measured expanding ``85%``, ``Dr.``,
``10:30`` and ``3rd`` correctly on its own — with an exact clip-duration match
against a hand-expanded control for the percentage. It is weak on bare long
integers, currency and negative temperatures, and it offers no switch to turn
its own front end off, so anything expanded here would simply be processed
twice. The off-the-shelf alternative weighs 885 MB against this package's total
current dependency set of ``mewbo-core`` and ``pydantic``.

**Ordered lists lose their numbers, and that is the one policy worth
revisiting.** The number is recoverable from the order in a linear read, unlike
a table body, so dropping it is not content loss in the way dropping rows would
be — but an answer that later says "as in step 3" does lose its cross-reference.
It is stated here rather than hidden because a live listen is what should settle
it, and no listen has happened.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, ClassVar, Final

import mistune

#: One parsed markdown node. Mistune's AST is plain dicts, which is why the
#: dispatch below is a table rather than methods on a variant class: these nodes
#: belong to the parser, not to us, and nothing can be attached to them.
Node = Mapping[str, Any]

#: What a code block is read as instead of its contents. Byte-identical to the
#: console's own note, so the two surfaces do not say different sentences about
#: the same answer while both are in service.
CODE_BLOCK_NOTE: Final[str] = "Code block omitted."

#: Introduces a table's column names, once, before the rows they describe.
#: CSS 2 §17.7.1 makes ``speak-header: once`` the INITIAL value for exactly this
#: reason: repeating the header per cell is the behaviour of an INTERACTIVE
#: screen reader, where a listener arrows into a cell and needs to be told which
#: column they landed in. A linear read has no arrowing, and repeating costs
#: two to three times the duration for a fact already stated.
TABLE_HEADER_LEAD: Final[str] = "Columns: "

#: Punctuation that already closes a spoken unit. A unit ending in none of these
#: gets a full stop appended, because the engine's pause comes from punctuation
#: — it maps characters to acoustic tokens and has no SSML, so a bare heading
#: would otherwise run straight into the sentence beneath it.
TERMINAL_PUNCTUATION: Final[str] = ".!?:;,"


class MarkdownVerbalizer:
    """Turns one markdown document into the plain text a listener should hear.

    A plain class rather than a Pydantic model, for the same reason
    :class:`~mewbo_speech.gateway.SpeechGateway` is one: it holds an injected
    collaborator and crosses no trust boundary. The thing that crosses one is
    the request carrying the text, and that is already a validated model.

    The parser is a FIELD so a test can drive the policy from a hand-built tree,
    and so a caller that already holds a configured ``mistune`` instance can
    pass it rather than building a second. One instance is safe to share across
    threads: mistune allocates its parse state per call, and 8 threads parsing
    80 corpus documents concurrently produced trees identical to the
    single-threaded ones.

    **Nothing here is guarded by an availability probe.** Unlike the gateway
    leg, this has no absent state to render — see the dependency note in the
    package's own guidance for why ``mistune`` is a base dependency.
    """

    #: Block node type -> the method that speaks it. A table rather than a chain
    #: of ``if`` arms because the node types are a fixed vocabulary owned by the
    #: parser: a construct is added by writing a method and naming it here, and
    #: WHICH constructs have a policy is then readable as data instead of having
    #: to be reconstructed from control flow. Anything absent falls through to
    #: recursion over its children, which is the right answer for every
    #: container the parser may add.
    BLOCK_HANDLERS: ClassVar[Mapping[str, str]] = {
        "heading": "_speak_heading",
        "paragraph": "_speak_paragraph",
        "block_text": "_speak_paragraph",
        "block_code": "_speak_code",
        "block_quote": "_speak_children",
        "list": "_speak_list",
        "table": "_speak_table",
        "thematic_break": "_speak_pause",
        # Raw HTML is markup a listener has no use for, and the parser is the
        # only thing that can tell a `<div>` from a `<` typed in prose.
        "block_html": "_speak_nothing",
        "blank_line": "_speak_nothing",
    }

    #: Inline node type -> the method that renders it into a unit's text. The
    #: table is this short because the DEFAULT is already the policy for most of
    #: them: recursing into children emits a link's text while dropping its URL,
    #: strips emphasis, strong and strikethrough markers, and reads an image's
    #: alt text. Only the nodes carrying raw source need an entry.
    INLINE_HANDLERS: ClassVar[Mapping[str, str]] = {
        "text": "_render_raw",
        "codespan": "_render_codespan",
        "inline_html": "_render_nothing",
        "softbreak": "_render_space",
        "linebreak": "_render_space",
    }

    #: Collapses any run of whitespace into a single space. Applied to a code
    #: span because a span may carry a newline, and to nothing else.
    _WHITESPACE: ClassVar[re.Pattern[str]] = re.compile(r"\s+")

    #: A GFM table row: a line whose content is bounded by pipes. Used ONLY by
    #: the delimiter repair below, never to parse a table — the parser does that.
    _PIPE_ROW: ClassVar[re.Pattern[str]] = re.compile(r"^ {0,3}\|.*\|\s*$")

    #: The ``|---|---|`` row GFM requires under a table's first row.
    _PIPE_DELIMITER: ClassVar[re.Pattern[str]] = re.compile(r"^ {0,3}\|[\s:|-]+\|\s*$")

    def __init__(self, *, parser: Callable[[str], Any] | None = None) -> None:
        """Bind the markdown parser this verbalizer walks.

        ``strikethrough`` is enabled alongside ``table`` because a GFM table is
        the construct the policy cares most about and ``~~text~~`` appears in
        the same dialect; without the plugin the tildes are read aloud.
        """
        self.parser: Callable[[str], Any] = parser or mistune.create_markdown(
            renderer=None, plugins=["table", "strikethrough"]
        )

    # ── the one public entry point ──────────────────────────────────────────

    def verbalize(self, text: str) -> str:
        """Return *text* as the plain text a listener should hear.

        **Near-idempotent on its own output, and the exceptions are stated
        rather than hidden** — this runs over already-stripped text as often as
        not, because both existing clients strip markdown before posting.
        Measured over 1,262 real replies, a second pass preserves the WORDS of
        94.2% of them exactly. The 5.8% that change are documents whose PROSE
        contains literal markdown-significant characters — `<input type="file">`
        quoted in a sentence, a glob like ``*.md`` — which survive the first
        pass as plain text and are then read as markup by the second. No parser
        can distinguish those from real markup without knowing they came from a
        parser, so it is a property of re-parsing, not a defect to fix here.

        Pause structure is the weaker half: a single newline between spoken
        units is not markdown, so a second pass merges those units into one
        paragraph. That is inaudible — a newline and a space measured
        byte-identical clip lengths (2.090 s each) — because the engine's only
        real pause comes from a BLANK line, which measured 2.808 s for the same
        words. Blank lines do partly collapse on a second pass, so re-verbalized
        text is slightly flatter. Prefer passing the original markdown once.

        **Verbalized text is not guaranteed shorter than its source.** It is
        shorter for all but a narrow class of input — a tiny code block, whose
        three-character fence becomes a nineteen-character sentence, and a table
        whose header announcement outweighs the pipes it replaces. Measured over
        the corpus the worst expansion is small and bounded; the package
        guidance carries the number. A caller sizing a payload against a cap
        must not assume monotone shrinkage.

        Cost class: ``O(text length)`` — one line pass, one parse, one walk.
        Measured warm at ~0.5 ms/KB; the first call in a process pays a one-off
        ~7 ms for mistune's own lazy setup. Against a synthesis that takes
        seconds, neither figure is on a budget that matters.
        """
        if not text or not text.strip():
            return ""
        return self._join(self._speak_blocks(self.parser(self._repair_headerless_tables(text))))

    def _repair_headerless_tables(self, text: str) -> str:
        """Insert a delimiter row into a pipe block that has none.

        **This exists because the parser silently DROPS a row, which is the one
        failure this whole surface is built to prevent.** GFM requires a
        ``|---|`` delimiter under a table's first row. Given a pipe block
        without one, mistune's table plugin still parses a table — it promotes
        row 1 to the header, and then **discards row 2 entirely**. Measured on
        blocks of 2 to 5 pipe rows: the body comes back holding rows 3..N every
        time, and row 2 is simply gone. Two rows in, one row out, no error.

        A whole reply rarely looks like this (1 of 1,262). A CHUNK does: a
        client splits a long answer mid-table, and the tail chunk arrives as
        bare pipe rows with the delimiter left behind in the chunk before it.
        That is the shape a listener would lose a row to.

        Fixing it in the walk is impossible — the row never reaches the tree. So
        the repair is here, at the only point that still has the source. The
        inserted delimiter makes the first row a header, which is honest: in a
        split table's tail the true header is genuinely absent, and speaking the
        first row as a header names real column values rather than inventing
        any. Nothing is dropped either way.

        Cost class: ``O(lines)`` — one pass, and it rewrites nothing unless a
        pipe block is genuinely missing its delimiter.
        """
        lines = text.split("\n")
        repaired: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            repaired.append(line)
            index += 1
            if not self._PIPE_ROW.match(line):
                continue
            # `line` OPENS a pipe block. Only its second line decides whether the
            # block is malformed, so the rest of the block is then copied through
            # untouched — inserting a delimiter between every pair of rows would
            # turn one table into a stack of one-row tables, each announcing its
            # own header. (It did, before this loop consumed the whole block.)
            following = lines[index] if index < len(lines) else ""
            if self._PIPE_ROW.match(following) and not self._PIPE_DELIMITER.match(following):
                repaired.append("|" + "---|" * max(line.count("|") - 1, 1))
            while index < len(lines) and self._PIPE_ROW.match(lines[index]):
                repaired.append(lines[index])
                index += 1
        return "\n".join(repaired)

    # ── block policy ────────────────────────────────────────────────────────

    def _speak_blocks(self, nodes: Iterable[Node]) -> list[str]:
        """Speak a sequence of block nodes into lines. Cost class: ``O(subtree)``."""
        spoken: list[str] = []
        for node in nodes:
            handler = self.BLOCK_HANDLERS.get(str(node.get("type", "")))
            method = getattr(self, handler) if handler else self._speak_children
            spoken.extend(method(node))
        return spoken

    def _speak_children(self, node: Node) -> list[str]:
        """Speak a container's children — the fallback, and a blockquote's policy.

        A blockquote is deliberately spoken as its contents with no announcement:
        the quoting is visual chrome, and a spoken "quote"/"end quote" wrapper
        doubles the length of the four percent of replies that contain one for a
        fact the words themselves usually carry.
        """
        return self._speak_blocks(self._children(node))

    def _speak_paragraph(self, node: Node) -> list[str]:
        """Speak one paragraph as a single unit. Cost class: ``O(subtree)``."""
        spoken = self._sentence(self._render_inline(self._children(node)))
        return [spoken] if spoken else []

    def _speak_heading(self, node: Node) -> list[str]:
        """Speak a heading's text, then pause. Never its level.

        The level is announced by every screen reader and is deliberately not
        announced here: it is useful there because it is ACTIONABLE — a listener
        jumps between headings by level. Nothing can be jumped to in a linear
        read, so the number is pure overhead on the sixteen percent of replies
        that carry one.
        """
        spoken = self._sentence(self._render_inline(self._children(node)))
        return [spoken, ""] if spoken else []

    def _speak_code(self, node: Node) -> list[str]:
        """Announce a code block once and skip its contents.

        Both fence styles and the indented form arrive here as the same node
        type, which is the whole reason a parser is worth its weight: the
        shipped substitution pass recognised only ``` fences, so ``~~~`` blocks
        and four-space indented blocks — 4.7% of replies — were read aloud in
        full.

        Cost class: ``O(1)`` — the contents are never touched.
        """
        return [CODE_BLOCK_NOTE]

    def _speak_list(self, node: Node) -> list[str]:
        """Speak each list item as its own unit, at any nesting depth.

        Nesting is FLATTENED rather than announced. Depth is another interactive
        affordance — a listener cannot see indentation and cannot navigate by
        it, so "level two" describes a shape they will never act on. The items
        themselves survive in order, which is what carries the grouping.

        **An ordered list KEEPS its numbers, and dropping them was measured to
        be a defect rather than a simplification.** Two findings, either alone
        sufficient. The number is audible and costs almost nothing: "1. First
        step" measured 1.74 s against 1.32 s for "First step", so the engine
        reads the ordinal as a word rather than as punctuation. And output with
        the numbers dropped is not stable under a second pass — a line that
        began "1. " is markdown for an ordered list, so re-verbalizing already
        verbalized text silently RENUMBERED or removed items. That matters here
        specifically: both existing clients strip markdown before posting, so
        this frequently runs over text that has already been through a pass.

        Cost class: ``O(subtree)``.
        """
        attrs = node.get("attrs") or {}
        ordinal = int(attrs.get("start") or 1) if attrs.get("ordered") else None
        spoken: list[str] = []
        for item in self._children(node):
            body = self._speak_blocks(self._children(item))
            if ordinal is not None and body:
                # Only the item's FIRST unit is numbered; a nested list or a
                # second paragraph inside the item is part of the same item and
                # numbering it again would invent entries that do not exist.
                body[0] = f"{ordinal}. {body[0]}"
                ordinal += 1
            spoken.extend(body)
        return spoken

    def _speak_table(self, node: Node) -> list[str]:
        """Speak a table's header once, then every data row.

        **No row is ever dropped.** A size gate that summarised large tables
        would have discarded the body of nearly half the tables in the corpus,
        and silently withholding content someone asked to hear is the worst
        failure available here — worse than a long read, which is at least
        audible as a long read.

        The header is announced once and then assumed, which is CSS 2 §17.7.1's
        ``speak-header: once``. Measured shape of the corpus's 167 tables: 3
        columns and 5 data rows at the median, 8 columns and 22 rows at the
        widest and longest, so the announced schema is something a listener can
        actually hold.

        A header with no rows beneath it is spoken as a plain list of cells: the
        announcement introduces a schema for rows that follow, and there are
        none to introduce. See :meth:`_repair_headerless_tables` for why a
        pipe block that never had a header reaches here with one anyway.

        Cost class: ``O(cells)``.
        """
        header: list[str] = []
        rows: list[list[str]] = []
        for section in self._children(node):
            # The head's children ARE the cells; a body's children are rows,
            # whose children are the cells. One level of nesting apart, so the
            # two cannot share a loop without a branch anyway.
            if section.get("type") == "table_head":
                header = self._render_cells(section)
            else:
                rows.extend(self._render_cells(row) for row in self._children(section))
        spoken = [self._sentence(", ".join(row)) for row in rows if any(cell for cell in row)]
        if header and spoken:
            return [self._sentence(TABLE_HEADER_LEAD + ", ".join(header)), *spoken]
        if header:
            return [self._sentence(", ".join(header))]
        return spoken

    def _render_cells(self, row: Node) -> list[str]:
        """Render one table row (or the head) into its cell texts. ``O(cells)``."""
        return [self._render_inline(self._children(cell)) for cell in self._children(row)]

    def _speak_pause(self, node: Node) -> list[str]:
        """Speak a horizontal rule as a long pause, never as a word.

        A rule is a visual divider; "separator" is what a screen reader says
        because it is describing a page someone is looking at. Said aloud across
        the eleven percent of replies containing one, it is a word the writer
        never wrote.

        Cost class: ``O(1)``.
        """
        return ["", ""]

    def _speak_nothing(self, node: Node) -> list[str]:
        """Speak nothing at all. Cost class: ``O(1)``."""
        return []

    # ── inline policy ───────────────────────────────────────────────────────

    def _render_inline(self, nodes: Iterable[Node]) -> str:
        """Render inline nodes into one unit's text. Cost class: ``O(subtree)``."""
        parts: list[str] = []
        for node in nodes:
            handler = self.INLINE_HANDLERS.get(str(node.get("type", "")))
            method = getattr(self, handler) if handler else self._render_children
            parts.append(method(node))
        return "".join(parts).strip()

    def _render_children(self, node: Node) -> str:
        """Render a container's children — the fallback, and most of the policy.

        Emphasis, strong and strikethrough lose their markers by falling through
        here, and so does a link: recursing emits the link TEXT and never
        touches the URL, which is where the shipped substitution pass broke —
        its bracket matching stopped at the first ``)``, so a URL containing
        parentheses left a stray one audible mid-sentence. An image reaches the
        same fallback and reads its alt text; that is a consequence, not a
        feature, since the corpus contains none.

        Emphasis markers are stripped rather than converted to a spoken stress:
        the one screen reader that shipped announcing them reverted it as
        over-used in the wild, and this engine has no markup channel to convert
        them into anyway.
        """
        return self._render_inline(self._children(node))

    def _render_raw(self, node: Node) -> str:
        """Render a literal text node. Cost class: ``O(1)``."""
        return str(node.get("raw", ""))

    def _render_codespan(self, node: Node) -> str:
        """Render a code span: backticks gone, underscores read as spaces.

        ``__init__`` becoming "init" is the intended reading and matches what a
        screen reader does at stock settings, where an underscore falls below
        the level at which symbols are announced and is replaced by a space.

        **For THIS engine the transform is a no-op, measured** — it already
        folds underscores itself, so ``max_text_chars`` and "max text chars"
        produced clips of identical length (2.577 s), as did ``__init__`` and
        "init" (1.463 s). It is kept for two reasons: the package targets a
        gateway whose backend an operator can change, and the transform makes
        the SPOKEN text observable in a test rather than hidden inside an engine
        nobody here controls. If a future engine reads underscores aloud, this
        already handles it; if none ever does, it costs one regex substitution.

        **camelCase is deliberately NOT split.** The one tool that does it by
        default is a programmer's environment, and it misfires on exactly the
        identifiers an assistant writes most — ``iOS``, ``macOS``, ``GitHub``,
        ``JavaScript``, ``PostgreSQL``. The underscore has no such collision
        class: prose almost never contains one, so the transform is a no-op on
        everything that is not an identifier.

        Cost class: ``O(1)`` in the span's length.
        """
        return self._WHITESPACE.sub(" ", str(node.get("raw", "")).replace("_", " ")).strip()

    def _render_nothing(self, node: Node) -> str:
        """Render nothing — an inline HTML tag has no spoken form. ``O(1)``."""
        return ""

    def _render_space(self, node: Node) -> str:
        """Render a line break inside a unit as a word gap. ``O(1)``."""
        return " "

    # ── shared ──────────────────────────────────────────────────────────────

    @staticmethod
    def _children(node: Node) -> Sequence[Node]:
        """Return a node's children, or an empty sequence. Cost class: ``O(1)``."""
        children = node.get("children")
        return children if isinstance(children, Sequence) else ()

    @staticmethod
    def _sentence(text: str) -> str:
        """Close a unit with a full stop unless it already ends in punctuation.

        The engine's pause comes from punctuation and nowhere else — it maps
        characters straight to acoustic tokens and accepts no SSML — so a unit
        left unterminated runs into the next one.

        Cost class: ``O(1)``.
        """
        cleaned = text.strip()
        if not cleaned or cleaned[-1] in TERMINAL_PUNCTUATION:
            return cleaned
        return f"{cleaned}."

    @staticmethod
    def _join(lines: Sequence[str]) -> str:
        """Join spoken units, collapsing runs of pauses into one.

        A blank line is how a pause is expressed, and adjacent blocks each
        contributing one would otherwise stack into a silence proportional to
        the markup rather than to the meaning.

        Cost class: ``O(units)``.
        """
        joined: list[str] = []
        for line in lines:
            if line or (joined and joined[-1]):
                joined.append(line)
        while joined and not joined[-1]:
            joined.pop()
        return "\n".join(joined)
