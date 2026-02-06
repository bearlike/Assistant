"""Search/replace block parsing adapted from Aider.

Source: https://github.com/Aider-AI/aider (commit 4bf56b77145b0be593ed48c3c90cdecead217496)
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_FENCE = ("`" * 3, "`" * 3)

HEAD = r"^<{5,9} SEARCH>?\s*$"
DIVIDER = r"^={5,9}\s*$"
UPDATED = r"^>{5,9} REPLACE\s*$"

HEAD_ERR = "<<<<<<< SEARCH"
DIVIDER_ERR = "======="
UPDATED_ERR = ">>>>>>> REPLACE"

triple_backticks = "`" * 3


@dataclass(frozen=True)
class EditBlock:
    """Structured representation of a search/replace edit."""

    path: str
    search: str
    replace: str


class EditBlockParseError(ValueError):
    """Raised when SEARCH/REPLACE blocks are malformed."""


class EditBlockApplyError(ValueError):
    """Raised when edits cannot be applied."""


@dataclass(frozen=True)
class AppliedEdit:
    """Outcome of applying a search/replace block."""

    path: str
    applied: bool
    created: bool


def parse_search_replace_blocks(
    content: str,
    *,
    valid_fnames: Iterable[str] | None = None,
    fence: tuple[str, str] = DEFAULT_FENCE,
) -> tuple[list[EditBlock], list[str]]:
    """Parse search/replace blocks from raw content.

    Args:
        content: Raw text containing SEARCH/REPLACE blocks.
        valid_fnames: Optional iterable of valid filenames for fuzzy matching.
        fence: Fence tuple used to detect code fences.

    Returns:
        Tuple of parsed edits and shell command blocks.
    """
    edits: list[EditBlock] = []
    shell_blocks: list[str] = []
    for item in find_original_update_blocks(
        content, fence=fence, valid_fnames=list(valid_fnames or [])
    ):
        if len(item) == 2:
            _, shell_block = item
            shell_blocks.append(shell_block)
            continue
        filename, original, updated = item
        if filename is None:
            shell_blocks.append(updated)
            continue
        edits.append(EditBlock(path=filename, search=original, replace=updated))
    return edits, shell_blocks


def apply_search_replace_blocks(
    content: str,
    *,
    root: str,
    valid_fnames: Iterable[str] | None = None,
    write: bool = False,
) -> list[AppliedEdit]:
    """Apply SEARCH/REPLACE blocks to files under a root directory.

    Args:
        content: Raw SEARCH/REPLACE block content.
        root: Root directory to resolve file paths against.
        valid_fnames: Optional list of valid filenames for fuzzy matching.
        write: When True, write changes to disk.

    Returns:
        List of applied edit outcomes.
    """
    edits, shell_blocks = parse_search_replace_blocks(
        content, valid_fnames=valid_fnames, fence=DEFAULT_FENCE
    )
    if shell_blocks:
        raise EditBlockApplyError("Shell command blocks are not supported by this tool.")

    root_path = Path(root).resolve()
    file_cache: dict[Path, str] = {}
    file_exists: dict[Path, bool] = {}
    created: set[Path] = set()
    results: list[AppliedEdit] = []

    for edit in edits:
        target_path = _resolve_target(root_path, edit.path)
        exists = file_exists.get(target_path)
        if exists is None:
            exists = target_path.exists()
            file_exists[target_path] = exists
        if target_path not in file_cache:
            file_cache[target_path] = target_path.read_text(encoding="utf-8") if exists else ""

        new_content = _compute_replacement(
            content=file_cache[target_path],
            before_text=edit.search,
            after_text=edit.replace,
            file_exists=exists,
            fence=DEFAULT_FENCE,
        )

        if new_content is None:
            suggestion = find_similar_lines(edit.search, file_cache[target_path])
            hint = ""
            if suggestion:
                hint = f"\nDid you mean to match:\n{suggestion}"
            raise EditBlockApplyError(f"SEARCH block failed to match in {edit.path}.{hint}")

        file_cache[target_path] = new_content
        created_flag = not exists and not edit.search.strip()
        if created_flag:
            created.add(target_path)
        results.append(AppliedEdit(path=edit.path, applied=True, created=created_flag))

    if write:
        for path, updated_content in file_cache.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(updated_content, encoding="utf-8")

    return results


def _resolve_target(root_path: Path, rel_path: str) -> Path:
    rel_path = rel_path.strip()
    candidate = Path(rel_path)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise EditBlockApplyError(
            f"Edit path '{rel_path}' resolves outside the project root."
        ) from exc
    return resolved


def _compute_replacement(
    *,
    content: str,
    before_text: str,
    after_text: str,
    file_exists: bool,
    fence: tuple[str, str] = DEFAULT_FENCE,
) -> str | None:
    before_text = strip_quoted_wrapping(before_text, fence=fence)
    after_text = strip_quoted_wrapping(after_text, fence=fence)

    if not file_exists and not before_text.strip():
        content = ""

    if content is None:
        return None

    if not before_text.strip():
        return content + after_text

    return replace_most_similar_chunk(content, before_text, after_text)


def prep(content: str) -> tuple[str, list[str]]:
    if content and not content.endswith("\n"):
        content += "\n"
    lines = content.splitlines(keepends=True)
    return content, lines


def perfect_or_whitespace(whole_lines, part_lines, replace_lines):
    res = perfect_replace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    res = replace_part_with_missing_leading_whitespace(whole_lines, part_lines, replace_lines)
    if res:
        return res


def perfect_replace(whole_lines, part_lines, replace_lines):
    part_tup = tuple(part_lines)
    part_len = len(part_lines)

    for i in range(len(whole_lines) - part_len + 1):
        whole_tup = tuple(whole_lines[i : i + part_len])
        if part_tup == whole_tup:
            res = whole_lines[:i] + replace_lines + whole_lines[i + part_len :]
            return "".join(res)


def replace_most_similar_chunk(whole, part, replace):
    whole, whole_lines = prep(whole)
    part, part_lines = prep(part)
    replace, replace_lines = prep(replace)

    res = perfect_or_whitespace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    if len(part_lines) > 2 and not part_lines[0].strip():
        skip_blank_line_part_lines = part_lines[1:]
        res = perfect_or_whitespace(whole_lines, skip_blank_line_part_lines, replace_lines)
        if res:
            return res

    try:
        res = try_dotdotdots(whole, part, replace)
        if res:
            return res
    except ValueError:
        pass

    return


def try_dotdotdots(whole, part, replace):
    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)

    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)

    if len(part_pieces) != len(replace_pieces):
        raise ValueError("Unpaired ... in SEARCH/REPLACE block")

    if len(part_pieces) == 1:
        return

    all_dots_match = all(part_pieces[i] == replace_pieces[i] for i in range(1, len(part_pieces), 2))

    if not all_dots_match:
        raise ValueError("Unmatched ... in SEARCH/REPLACE block")

    part_pieces = [part_pieces[i] for i in range(0, len(part_pieces), 2)]
    replace_pieces = [replace_pieces[i] for i in range(0, len(replace_pieces), 2)]

    pairs = zip(part_pieces, replace_pieces)
    for part_piece, replace_piece in pairs:
        if not part_piece and not replace_piece:
            continue

        if not part_piece and replace_piece:
            if not whole.endswith("\n"):
                whole += "\n"
            whole += replace_piece
            continue

        if whole.count(part_piece) == 0:
            raise ValueError
        if whole.count(part_piece) > 1:
            raise ValueError

        whole = whole.replace(part_piece, replace_piece, 1)

    return whole


def replace_part_with_missing_leading_whitespace(whole_lines, part_lines, replace_lines):
    leading = [len(p) - len(p.lstrip()) for p in part_lines if p.strip()] + [
        len(p) - len(p.lstrip()) for p in replace_lines if p.strip()
    ]

    if leading and min(leading):
        num_leading = min(leading)
        part_lines = [p[num_leading:] if p.strip() else p for p in part_lines]
        replace_lines = [p[num_leading:] if p.strip() else p for p in replace_lines]

    num_part_lines = len(part_lines)

    for i in range(len(whole_lines) - num_part_lines + 1):
        add_leading = match_but_for_leading_whitespace(
            whole_lines[i : i + num_part_lines], part_lines
        )

        if add_leading is None:
            continue

        replace_lines = [add_leading + rline if rline.strip() else rline for rline in replace_lines]
        whole_lines = whole_lines[:i] + replace_lines + whole_lines[i + num_part_lines :]
        return "".join(whole_lines)

    return None


def match_but_for_leading_whitespace(whole_lines, part_lines):
    num = len(whole_lines)

    if not all(whole_lines[i].lstrip() == part_lines[i].lstrip() for i in range(num)):
        return

    add = set(
        whole_lines[i][: len(whole_lines[i]) - len(part_lines[i])]
        for i in range(num)
        if whole_lines[i].strip()
    )

    if len(add) != 1:
        return

    return add.pop()


def replace_closest_edit_distance(whole_lines, part, part_lines, replace_lines):
    similarity_thresh = 0.8

    max_similarity = 0
    most_similar_chunk_start = -1
    most_similar_chunk_end = -1

    scale = 0.1
    min_len = math.floor(len(part_lines) * (1 - scale))
    max_len = math.ceil(len(part_lines) * (1 + scale))

    for length in range(min_len, max_len):
        for i in range(len(whole_lines) - length + 1):
            chunk = whole_lines[i : i + length]
            chunk = "".join(chunk)

            similarity = SequenceMatcher(None, chunk, part).ratio()

            if similarity > max_similarity and similarity:
                max_similarity = similarity
                most_similar_chunk_start = i
                most_similar_chunk_end = i + length

    if max_similarity < similarity_thresh:
        return

    modified_whole = (
        whole_lines[:most_similar_chunk_start]
        + replace_lines
        + whole_lines[most_similar_chunk_end:]
    )
    modified_whole = "".join(modified_whole)

    return modified_whole


def strip_quoted_wrapping(res: str, fence: tuple[str, str] = DEFAULT_FENCE) -> str:
    if not res:
        return res

    lines = res.splitlines()

    if lines and lines[0].startswith(fence[0]) and lines[-1].startswith(fence[1]):
        lines = lines[1:-1]

    res = "\n".join(lines)
    if res and res[-1] != "\n":
        res += "\n"

    return res


def strip_filename(filename, fence):
    filename = filename.strip()

    if filename == "...":
        return

    start_fence = fence[0]
    if filename.startswith(start_fence):
        candidate = filename[len(start_fence) :]
        if candidate and ("." in candidate or "/" in candidate):
            return candidate
        return

    if filename.startswith(triple_backticks):
        candidate = filename[len(triple_backticks) :]
        if candidate and ("." in candidate or "/" in candidate):
            return candidate
        return

    filename = filename.rstrip(":")
    filename = filename.lstrip("#")
    filename = filename.strip()
    filename = filename.strip("`")
    filename = filename.strip("*")

    return filename


missing_filename_err = (
    "Bad/missing filename. The filename must be alone on the line before the opening fence"
    " {fence[0]}"
)


def find_original_update_blocks(content, fence=DEFAULT_FENCE, valid_fnames=None):
    lines = content.splitlines(keepends=True)
    i = 0
    current_filename = None

    head_pattern = re.compile(HEAD)
    divider_pattern = re.compile(DIVIDER)
    updated_pattern = re.compile(UPDATED)

    while i < len(lines):
        line = lines[i]

        shell_starts = [
            "```bash",
            "```sh",
            "```shell",
            "```cmd",
            "```batch",
            "```powershell",
            "```ps1",
            "```zsh",
            "```fish",
            "```ksh",
            "```csh",
            "```tcsh",
        ]

        next_is_editblock = (
            i + 1 < len(lines)
            and head_pattern.match(lines[i + 1].strip())
            or i + 2 < len(lines)
            and head_pattern.match(lines[i + 2].strip())
        )

        if any(line.strip().startswith(start) for start in shell_starts) and not next_is_editblock:
            shell_content = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                shell_content.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1

            yield None, "".join(shell_content)
            continue

        if head_pattern.match(line.strip()):
            try:
                if i + 1 < len(lines) and divider_pattern.match(lines[i + 1].strip()):
                    filename = find_filename(lines[max(0, i - 3) : i], fence, None)
                else:
                    filename = find_filename(lines[max(0, i - 3) : i], fence, valid_fnames)

                if not filename:
                    if current_filename:
                        filename = current_filename
                    else:
                        raise ValueError(missing_filename_err.format(fence=fence))

                current_filename = filename

                original_text = []
                i += 1
                while i < len(lines) and not divider_pattern.match(lines[i].strip()):
                    original_text.append(lines[i])
                    i += 1

                if i >= len(lines) or not divider_pattern.match(lines[i].strip()):
                    raise ValueError(f"Expected `{DIVIDER_ERR}`")

                updated_text = []
                i += 1
                while i < len(lines) and not (
                    updated_pattern.match(lines[i].strip())
                    or divider_pattern.match(lines[i].strip())
                ):
                    updated_text.append(lines[i])
                    i += 1

                if i >= len(lines) or not (
                    updated_pattern.match(lines[i].strip())
                    or divider_pattern.match(lines[i].strip())
                ):
                    raise ValueError(f"Expected `{UPDATED_ERR}` or `{DIVIDER_ERR}`")

                yield filename, "".join(original_text), "".join(updated_text)

            except ValueError as exc:
                processed = "".join(lines[: i + 1])
                err = exc.args[0]
                raise EditBlockParseError(f"{processed}\n^^^ {err}") from exc

        i += 1


def find_filename(lines, fence, valid_fnames):
    if valid_fnames is None:
        valid_fnames = []

    lines.reverse()
    lines = lines[:3]

    filenames = []
    for line in lines:
        filename = strip_filename(line, fence)
        if filename:
            filenames.append(filename)

        if not line.startswith(fence[0]) and not line.startswith(triple_backticks):
            break

    if not filenames:
        return

    for fname in filenames:
        if fname in valid_fnames:
            return fname

    for fname in filenames:
        for vfn in valid_fnames:
            if fname == Path(vfn).name:
                return vfn

    for fname in filenames:
        close_matches = difflib.get_close_matches(fname, valid_fnames, n=1, cutoff=0.8)
        if len(close_matches) == 1:
            return close_matches[0]

    for fname in filenames:
        if "." in fname:
            return fname

    if filenames:
        return filenames[0]


def find_similar_lines(search_lines, content_lines, threshold=0.6):
    search_lines = search_lines.splitlines()
    content_lines = content_lines.splitlines()

    best_ratio = 0
    best_match = None

    for i in range(len(content_lines) - len(search_lines) + 1):
        chunk = content_lines[i : i + len(search_lines)]
        ratio = SequenceMatcher(None, search_lines, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = chunk
            best_match_i = i

    if best_ratio < threshold:
        return ""

    if best_match and best_match[0] == search_lines[0] and best_match[-1] == search_lines[-1]:
        return "\n".join(best_match)

    N = 5
    best_match_end = min(len(content_lines), best_match_i + len(search_lines) + N)
    best_match_i = max(0, best_match_i - N)

    best = content_lines[best_match_i:best_match_end]
    return "\n".join(best)
