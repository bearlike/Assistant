# CLI JSON to live rows

Use this for a CLI that really emits JSON. `repos.py` keeps I/O at the edge and makes every
assumption about the decoded response explicit; a changed CLI response fails the run instead of
rendering guessed data.

```python
{
    "name": "repository-list",
    "wake_prompt": "Return current repositories from the forge CLI.",
    "mode": "code",
    "tier": "render",
    "entrypoint": "pipelines/repos.py",
    "on_demand": True,
    "allow_exec": ["gh"],
    "allow_egress": ["github.com"],
    "result": {
        "media": "json",
        "json_schema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "owner": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["name", "owner", "url"],
                "additionalProperties": False,
            },
        },
    },
    "samples": [{"label": "empty parameters", "params": {}}],
}
```

`ctx.exec` takes an argv list; a non-zero exit is data the pipeline must reject itself. Do not
catch it and return `[]`: that reports a successful run, weakens submit verification, and ships a
broken live view. The pipeline linter rejects broad catches that swallow guarded `ctx` failures.
