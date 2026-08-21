import csv
import io


def expense_docs(path: str, text: str) -> list[tuple[str, dict]]:
    """Make schema-ready rows before the I/O edge writes them."""
    rows = []
    for line_number, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        # Name the missing column rather than reporting that "a field" was absent:
        # the author fixing this reads the message, not this loop.
        fields = {}
        for column in ("id", "date", "amount", "category"):
            value = row.get(column)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{path} line {line_number} is missing {column!r}")
            fields[column] = value
        expense_id = fields["id"]
        try:
            amount = float(fields["amount"])
        except ValueError:
            raise ValueError(
                f"{path} line {line_number} has a non-numeric amount {fields['amount']!r}"
            ) from None
        rows.append(
            (
                f"{path}:{expense_id}",
                {
                    "date": fields["date"],
                    "amount": amount,
                    "category": fields["category"],
                    "source_file": path,
                },
            )
        )
    return rows


def optional_note(ctx) -> str | None:
    """Tolerate only an absent optional file; every other context failure is a bug."""
    try:
        return ctx.read_file("exports/README.txt")
    except Exception as exc:
        if getattr(exc, "code", None) == "read":
            return None
        raise


def run(params: dict, ctx) -> dict:
    files = ctx.glob("exports/*.csv")
    written = 0
    expenses = ctx.collection("expenses")
    for path in files:
        for key, doc in expense_docs(path, ctx.read_file(path)):
            expenses.upsert(key, doc)
            written += 1
    return {
        "files_seen": len(files),
        "rows_written": written,
        "has_optional_note": optional_note(ctx) is not None,
    }
