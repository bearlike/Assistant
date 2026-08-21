# User-input form to collection

A served app can write only through a `user_writable` code pipeline. The platform validates the
form against `params_schema` before `run`, and `ctx.collection` validates the stored document. The
pipeline still checks its own semantic invariant—non-blank after normalization—where it is made.

Collection and pipeline declaration:

```python
{
    "collections": [{
        "name": "notes",
        "json_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "pinned": {"type": "boolean"}},
            "required": ["text", "pinned"],
            "additionalProperties": False,
        },
    }],
    "pipelines": [{
        "name": "add-note",
        "wake_prompt": "Store a user-submitted note.",
        "mode": "code",
        "tier": "materialize",
        "entrypoint": "pipelines/add_note.py",
        "on_demand": True,
        "user_writable": True,
        "params_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "pinned": {"type": "boolean"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "result": {
            "media": "json",
            "json_schema": {
                "type": "object",
                "properties": {"saved": {"type": "string"}},
                "required": ["saved"],
                "additionalProperties": False,
            },
        },
        "samples": [{"label": "pinned note", "params": {"text": "Review draft", "pinned": True}}],
    }],
}
```

The frontend submits `app.pipelines.submit("add-note", {"text": text, "pinned": pinned})`, then
re-reads the collection. Do not use a broad `except` to claim the note saved after a rejected
write; that is precisely the false success the pipeline linter protects against.
