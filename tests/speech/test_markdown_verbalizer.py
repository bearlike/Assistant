"""Behaviour tests for :class:`~mewbo_speech.MarkdownVerbalizer`.

Two layers, and the split is deliberate. The first replays REAL assistant
replies from ``verbalizer_corpus.json``, sampled out of the production
transcript store — a hand-authored fixture is written to match the
implementation and therefore cannot surprise it, which is exactly why these were
sampled instead. The second pins each individual policy on a one-line document,
so a failure names the rule that broke rather than a paragraph that changed.

The named breakages below are all REAL, every one of them measured against the
substitution pass this replaces: a URL containing parentheses left a stray
bracket audible, a ``~~~`` fence and a four-space indented block were read out
in full, raw HTML tags were spoken, a setext underline was read as a row of
equals signs, and ``3 * 4 * 5`` lost its asterisks to the emphasis rule. Each
has a test here that fails if the behaviour returns.
"""

import json
import re
from pathlib import Path

import pytest
from mewbo_speech import CODE_BLOCK_NOTE, TABLE_HEADER_LEAD, MarkdownVerbalizer

#: Real assistant replies, one per construct the policy covers.
CORPUS = json.loads((Path(__file__).parent / "verbalizer_corpus.json").read_text())["documents"]


@pytest.fixture
def verbalizer() -> MarkdownVerbalizer:
    """One verbalizer per test — construction is cheap and state-free."""
    return MarkdownVerbalizer()


class TestTheBreakagesTheRegexPathShipped:
    """Each of these was audible in production before a parser replaced regex."""

    def test_a_url_containing_parentheses_leaves_no_stray_bracket(self, verbalizer):
        # The substitution pass matched `[^)]*` for the URL, so it stopped at the
        # first `)` INSIDE the URL and left the closing one in the prose.
        spoken = verbalizer.verbalize(
            "See the [docs](https://en.wikipedia.org/wiki/Foo_(bar)) for details."
        )
        assert spoken == "See the docs for details."
        assert ")" not in spoken

    def test_a_tilde_fence_is_announced_not_read_aloud(self, verbalizer):
        spoken = verbalizer.verbalize("Before.\n\n~~~\nrm -rf /\n~~~\n\nAfter.")
        assert "rm -rf" not in spoken
        assert CODE_BLOCK_NOTE in spoken
        assert spoken.startswith("Before.") and spoken.endswith("After.")

    def test_four_space_indented_code_is_announced_not_read_aloud(self, verbalizer):
        spoken = verbalizer.verbalize("Text:\n\n    def f():\n        return 1\n\nDone.")
        assert "def f()" not in spoken
        assert spoken == f"Text:\n{CODE_BLOCK_NOTE}\nDone."

    def test_html_tags_are_not_spoken(self, verbalizer):
        assert verbalizer.verbalize("<div class='x'>hello</div>\n\nafter") == "after."
        assert verbalizer.verbalize("Use <code>x</code> now.") == "Use x now."

    def test_a_setext_underline_is_a_heading_not_a_row_of_equals_signs(self, verbalizer):
        spoken = verbalizer.verbalize("Title\n=====\n\nbody")
        assert "=" not in spoken
        assert spoken.startswith("Title.")

    def test_multiplication_asterisks_survive(self, verbalizer):
        # The emphasis rule ate the asterisks, so "3 * 4 * 5" was heard as
        # "3  4  5" — three numbers with no operator between them.
        assert verbalizer.verbalize("3 * 4 * 5 = 60.") == "3 * 4 * 5 = 60."

    def test_a_dunder_identifier_reads_as_its_name(self, verbalizer):
        # This one was already CORRECT and is pinned so it stays that way.
        assert verbalizer.verbalize("Call `__init__` first.") == "Call init first."


class TestRealAssistantReplies:
    """Replays of production transcript text — the constructs as they occur."""

    @pytest.mark.parametrize("name", sorted(CORPUS))
    def test_every_corpus_document_verbalizes_to_speakable_text(self, verbalizer, name):
        """No markdown survives, and nothing is emitted that cannot be spoken."""
        spoken = verbalizer.verbalize(CORPUS[name])
        assert spoken.strip(), f"{name} verbalized to nothing"
        assert "```" not in spoken and "~~~" not in spoken
        assert not re.search(r"^\s*\|", spoken, re.M), "table pipes survived"
        assert not re.search(r"^\s*#{1,6}\s", spoken, re.M), "heading markers survived"
        assert not re.search(r"^\s*>\s", spoken, re.M), "blockquote markers survived"
        assert not re.search(r"</?[a-zA-Z][a-zA-Z0-9-]*\s*/?>", spoken), "an HTML tag survived"

    def test_a_real_table_announces_its_header_once_and_keeps_every_row(self, verbalizer):
        spoken = verbalizer.verbalize(CORPUS["table_ci"])
        assert spoken.count(TABLE_HEADER_LEAD) == 1
        assert f"{TABLE_HEADER_LEAD}Check, Result." in spoken
        # Three data rows in the source; all three are spoken, none summarised.
        assert "Ruff linting" in spoken and "pytest" in spoken and "mkdocs build" in spoken
        assert "Result" not in spoken.split("\n", 2)[2], "the header repeated per row"

    def test_a_real_nested_list_speaks_every_item_at_every_depth(self, verbalizer):
        spoken = verbalizer.verbalize(CORPUS["nested_scg"])
        for item in ("Nodes: 9.", "Edges: 37.", "Recipes: 8.", "Source count: 9."):
            assert item in spoken
        # Depth is flattened, never announced.
        assert "level" not in spoken.lower()

    def test_a_real_heading_ladder_never_announces_a_level(self, verbalizer):
        spoken = verbalizer.verbalize(CORPUS["heading_ladder"])
        for heading in ("Alpha Header.", "Beta Header.", "Gamma Header.", "Delta Header."):
            assert heading in spoken
        assert "heading" not in spoken.lower().replace("header", "")

    def test_a_real_horizontal_rule_becomes_a_pause_not_a_word(self, verbalizer):
        spoken = verbalizer.verbalize(CORPUS["hrule_table"])
        assert "separator" not in spoken.lower()
        assert "---" not in spoken
        assert "\n\n" in spoken, "the rule left no pause behind"

    def test_a_real_blockquote_is_spoken_without_announcing_itself(self, verbalizer):
        spoken = verbalizer.verbalize(CORPUS["blockquote_error"])
        assert "unable to write credential store" in spoken
        assert "quote" not in spoken.lower()


class TestPerConstructPolicy:
    """One rule per test, on the smallest document that exercises it."""

    def test_a_heading_is_followed_by_a_pause(self, verbalizer):
        assert verbalizer.verbalize("## Results\n\nAll green.") == "Results.\n\nAll green."

    def test_emphasis_markers_are_stripped_not_converted(self, verbalizer):
        spoken = verbalizer.verbalize("This is **bold** and *italic* and ~~gone~~.")
        assert spoken == "This is bold and italic and gone."
        assert "emphasis" not in spoken and "<" not in spoken

    def test_a_link_speaks_its_text_and_drops_its_url(self, verbalizer):
        spoken = verbalizer.verbalize("Read [the guide](https://example.com/a/b?c=d).")
        assert spoken == "Read the guide."
        assert "example.com" not in spoken

    def test_a_fenced_block_is_announced_exactly_once(self, verbalizer):
        spoken = verbalizer.verbalize("```python\nprint(1)\nprint(2)\n```")
        assert spoken == CODE_BLOCK_NOTE
        assert spoken.count(CODE_BLOCK_NOTE) == 1

    def test_list_items_are_separate_spoken_units(self, verbalizer):
        assert verbalizer.verbalize("- one\n- two\n- three") == "one.\ntwo.\nthree."

    def test_nested_list_items_are_flattened_in_order(self, verbalizer):
        assert verbalizer.verbalize("- one\n  - two\n    - three\n- four") == (
            "one.\ntwo.\nthree.\nfour."
        )

    def test_an_ordered_list_keeps_its_numbers(self, verbalizer):
        # Two measurements say keep them: the engine speaks the ordinal (1.74 s
        # for "1. First step" against 1.32 s without it), and dropping it made
        # the output re-parse as a list on a second pass and lose the number.
        assert verbalizer.verbalize("1. First step\n2. Second step") == (
            "1. First step.\n2. Second step."
        )

    def test_an_ordered_list_honours_its_start(self, verbalizer):
        assert verbalizer.verbalize("5. five\n6. six") == "5. five.\n6. six."

    def test_only_an_items_first_unit_is_numbered(self, verbalizer):
        # A nested list inside item 1 belongs to item 1; numbering it again
        # would invent entries the source does not contain.
        spoken = verbalizer.verbalize("1. outer\n   - inner\n2. next")
        assert spoken == "1. outer.\ninner.\n2. next."

    def test_a_table_body_is_never_dropped_however_large(self, verbalizer):
        rows = "\n".join(f"| item{n} | {n} |" for n in range(40))
        spoken = verbalizer.verbalize(f"| Name | Count |\n|---|---|\n{rows}")
        assert spoken.count(TABLE_HEADER_LEAD) == 1
        for n in range(40):
            assert f"item{n}, {n}." in spoken

    def test_a_lone_pipe_row_is_spoken_as_written(self, verbalizer):
        # One row is not a table, and the parser does not treat it as one.
        assert "just one" in verbalizer.verbalize("| just one |")

    def test_a_horizontal_rule_emits_a_pause_and_no_word(self, verbalizer):
        assert verbalizer.verbalize("Section A.\n\n---\n\nSection B.") == (
            "Section A.\n\nSection B."
        )

    def test_a_code_span_keeps_camel_case_intact(self, verbalizer):
        # Splitting camelCase misfires on exactly the identifiers an assistant
        # writes most, so only the underscore transform applies.
        assert verbalizer.verbalize("Check `getUserById` and `iOS`.") == (
            "Check getUserById and iOS."
        )

    def test_a_code_span_reads_underscores_as_spaces(self, verbalizer):
        assert verbalizer.verbalize("Run `snake_case_name`.") == "Run snake case name."

    def test_an_unterminated_fence_is_still_announced(self, verbalizer):
        # Normal on a streaming buffer: the closing marker has not arrived yet.
        assert verbalizer.verbalize("```python\nprint(1)") == CODE_BLOCK_NOTE

    def test_blank_and_whitespace_only_input_verbalize_to_nothing(self, verbalizer):
        assert verbalizer.verbalize("") == ""
        assert verbalizer.verbalize("   \n\n  ") == ""

    def test_markup_only_input_verbalizes_to_nothing(self, verbalizer):
        # The caller decides what to do about it; see the route's fallback.
        assert verbalizer.verbalize("---") == ""
        assert verbalizer.verbalize("<div></div>") == ""

    def test_every_spoken_unit_ends_in_punctuation(self, verbalizer):
        # The engine takes its pauses from punctuation and accepts no SSML, so
        # an unterminated unit runs into the next one.
        spoken = verbalizer.verbalize(CORPUS["nested_scg"])
        for unit in (line for line in spoken.split("\n") if line):
            assert unit[-1] in ".!?:;,", f"unit does not close: {unit!r}"


class TestPropertiesOverTheWholeCorpus:
    """Invariants that must hold for every document, not just the sampled ones."""

    @pytest.mark.parametrize("name", sorted(CORPUS))
    def test_a_second_pass_preserves_every_word(self, verbalizer, name):
        """Re-verbalizing must not lose, add or renumber a single word.

        Load-bearing rather than tidy: both clients strip markdown themselves
        today, so this runs over already-stripped text as often as not.

        WORDS, not the exact string — pause structure legitimately flattens,
        because a single newline between units is not markdown and a second
        pass merges them. That is inaudible (a newline and a space measured
        identical clip lengths). Losing a word is not, and this caught a real
        one: with ordinals dropped, a line beginning "1. " re-parsed as an
        ordered list and the number vanished on the second pass.
        """
        once = verbalizer.verbalize(CORPUS[name])
        assert verbalizer.verbalize(once).split() == once.split()

    @pytest.mark.parametrize("name", sorted(CORPUS))
    def test_no_markdown_delimiter_survives(self, verbalizer, name):
        spoken = verbalizer.verbalize(CORPUS[name])
        assert "**" not in spoken
        assert "`" not in spoken
        assert not re.search(r"\]\(", spoken), "a link's parentheses survived"


class TestADelimiterlessPipeBlockLosesNoRow:
    """The parser DROPS a row from a pipe block with no ``|---|`` delimiter.

    Given such a block, mistune's table plugin still parses a table: it promotes
    row 1 to the header and then discards row 2 outright — measured on blocks of
    2 to 5 rows, the body comes back holding rows 3..N every time. Two rows in,
    one row out, no error and nothing in a log.

    A whole assistant reply almost never looks like this (1 of 1,262 measured).
    A CHUNK does: a client splitting a long answer mid-table sends a tail of
    bare pipe rows, with the delimiter left behind in the previous chunk. So
    this is the exact shape a listener would lose a row to, and dropping content
    is the failure the entire table policy exists to prevent.
    """

    def test_a_two_row_fragment_keeps_both_rows(self, verbalizer):
        # The regression: this returned "a, b." — row two gone, silently.
        assert verbalizer.verbalize("| a | b |\n| c | d |") == "Columns: a, b.\nc, d."

    def test_a_split_tables_tail_keeps_every_row(self, verbalizer):
        spoken = verbalizer.verbalize("| beta | 12 | failed |\n| gamma | 3 | ok |")
        assert "beta, 12, failed" in spoken
        assert "gamma, 3, ok" in spoken

    @pytest.mark.parametrize("rows", [2, 3, 4, 5])
    def test_no_row_is_lost_at_any_block_length(self, verbalizer, rows):
        source = "\n".join(f"| r{n} | v{n} |" for n in range(rows))
        spoken = verbalizer.verbalize(source)
        for n in range(rows):
            assert f"r{n}" in spoken, f"row {n} vanished from a {rows}-row block"

    def test_a_well_formed_table_is_untouched_by_the_repair(self, verbalizer):
        """The repair must not fire where GFM is already satisfied.

        Inserting a delimiter between every PAIR of rows turns one table into a
        stack of one-row tables, each announcing its own header. An earlier cut
        of the repair did exactly that; this is the test that caught it.
        """
        spoken = verbalizer.verbalize("| Name | Cost |\n|---|---|\n| a | 1 |\n| b | 2 |")
        assert spoken == "Columns: Name, Cost.\na, 1.\nb, 2."
        assert spoken.count(TABLE_HEADER_LEAD) == 1
        assert "---" not in spoken

    def test_an_alignment_delimiter_still_counts_as_a_delimiter(self, verbalizer):
        assert verbalizer.verbalize("| A | B |\n|:--|--:|\n| 1 | 2 |") == (
            "Columns: A, B.\n1, 2."
        )

    def test_two_separate_tables_stay_separate(self, verbalizer):
        spoken = verbalizer.verbalize("| A |\n|---|\n| 1 |\n\ntext\n\n| B |\n|---|\n| 2 |")
        assert spoken == "Columns: A.\n1.\ntext.\nColumns: B.\n2."

    def test_a_pipe_in_ordinary_prose_is_not_a_table(self, verbalizer):
        assert verbalizer.verbalize("Use a | b for or.") == "Use a | b for or."


class TestLengthIsNotMonotone:
    """Verbalization can LENGTHEN text, and a caller must not assume otherwise.

    The tempting claim — "verbalized text is always shorter, so a chunk under a
    client's limit stays under it" — is FALSE, and these tests exist so nobody
    re-derives it from the common case. Measured over 1,262 real assistant
    replies it lengthens 29.6% of them; the growth is small (median ratio 0.99,
    p90 1.05, largest real growth 4 characters) but it is not zero, and the
    adversarial case is 2.5x.
    """

    def test_a_tiny_code_block_expands(self, verbalizer):
        source = "```\na\n```"
        spoken = verbalizer.verbalize(source)
        assert len(spoken) > len(source), "the fence-to-sentence expansion is gone"
        assert spoken == CODE_BLOCK_NOTE

    def test_a_tiny_table_expands_by_its_header_announcement(self, verbalizer):
        source = "|a|b|\n|-|-|\n|1|2|"
        assert len(verbalizer.verbalize(source)) > len(source)

    def test_a_document_of_empty_fences_is_the_adversarial_worst_case(self, verbalizer):
        # Every three-character fence pair becomes a nineteen-character
        # sentence. This is the shape the route's post-verbalization ceiling
        # exists to refuse, and the ratio is what sized it.
        source = "```\n```\n" * 100
        ratio = len(verbalizer.verbalize(source)) / len(source)
        assert ratio > 2.0, "the worst case shrank; the route's ceiling can be retuned"

    def test_the_common_case_still_shrinks(self, verbalizer):
        """Stated as a property so the honest exception above is not read as the rule."""
        spoken = verbalizer.verbalize(CORPUS["table_ci"])
        assert len(spoken) < len(CORPUS["table_ci"])


class TestTheParserIsInjectable:
    """The collaborator is a FIELD, so the policy is drivable without markdown."""

    def test_a_scripted_parser_drives_the_policy_directly(self):
        # Proves the walk reads the tree and nothing else — no re-parse, no
        # second look at the source string.
        tree = [
            {"type": "heading", "attrs": {"level": 2}, "children": [{"type": "text", "raw": "Hi"}]},
            {"type": "paragraph", "children": [{"type": "codespan", "raw": "a_b"}]},
        ]
        verbalizer = MarkdownVerbalizer(parser=lambda _text: tree)
        assert verbalizer.verbalize("ignored") == "Hi.\n\na b."

    def test_an_unknown_node_type_falls_through_to_its_children(self):
        """A construct a future mistune adds must not silently vanish."""
        tree = [
            {
                "type": "some_future_container",
                "children": [{"type": "paragraph", "children": [{"type": "text", "raw": "kept"}]}],
            }
        ]
        assert MarkdownVerbalizer(parser=lambda _t: tree).verbalize("x") == "kept."
