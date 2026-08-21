# File tree to collection

Use a materialize pipeline for a durable snapshot the frontend reads from a collection. `expenses.py`
globs each run, then holds parsing in a pure helper and leaves all reads/writes at the edge. The
stable key includes provenance, so a rerun updates the same document rather than duplicating it.

Collection schema:

```python
{
    "name": "expenses",
    "json_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "amount": {"type": "number"},
            "category": {"type": "string"},
            "source_file": {"type": "string"},
        },
        "required": ["date", "amount", "category", "source_file"],
        "additionalProperties": False,
    },
}
```

Pipeline declaration:

```python
{
    "name": "ingest-expenses",
    "wake_prompt": "Parse workspace expense CSV files into the expenses collection.",
    "mode": "code",
    "tier": "materialize",
    "entrypoint": "pipelines/expenses.py",
    "schedule": {"kind": "time.cron", "cron": "0 */6 * * *"},
    "cache_mode": "source",
    "result": {
        "media": "json",
        "json_schema": {
            "type": "object",
            "properties": {
                "files_seen": {"type": "integer", "minimum": 0},
                "rows_written": {"type": "integer", "minimum": 0},
                "has_optional_note": {"type": "boolean"},
            },
            "required": ["files_seen", "rows_written", "has_optional_note"],
            "additionalProperties": False,
        },
    },
    "samples": [{"label": "empty parameters", "params": {}}],
}
```

`cache_mode="source"` recomputes when the recorded glob set or source file stats change. A schema
can validate a document, not a missing CSV column, so the helper raises at the malformed row.
`optional_note()` demonstrates the sole tolerable catch shape: it accepts only `code == "read"` for
one optional file and re-raises traversal, workspace, and every other failure. Never turn a
`ctx.glob` or `ctx.read_file` error into an empty list: a broad swallowed error is linted as an
unsafe green run.
