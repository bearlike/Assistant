import re

_BRANCH_LINE = re.compile(r"^(?P<name>\S+)\s+(?P<sha>[0-9a-f]{7,64})\s+(?P<subject>.+)$")


def parse_branches(stdout: str) -> list[dict]:
    """Parse the exact line format requested from git, refusing ambiguous rows."""
    rows = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        match = _BRANCH_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"git branch output line {line_number} has an unexpected shape: {line!r}")
        rows.append(match.groupdict())
    return rows


def run(params: dict, ctx) -> list[dict]:
    response = ctx.exec(["git", "for-each-ref", "--format=%(refname:short) %(objectname:short) %(subject)"])
    if response["returncode"] != 0:
        raise RuntimeError(f"git for-each-ref failed: {response['stderr']}")
    return parse_branches(response["stdout"])
