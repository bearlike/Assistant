# CLI text to live rows

Use this when a CLI does **not** emit JSON. `branches.py` requests one stable line format, parses it
with a full-line expression, and rejects anything it cannot prove. Splitting on whitespace and
hoping the fields line up silently turns an upstream format change into wrong rows.

```python
{
    "name": "branch-list",
    "wake_prompt": "Return the workspace's current branch summaries.",
    "mode": "code",
    "tier": "render",
    "entrypoint": "pipelines/branches.py",
    "on_demand": True,
    "allow_exec": ["git"],
    "result": {
        "media": "csv",
        "columns": ["name", "sha", "subject"],
    },
    "samples": [{"label": "empty parameters", "params": {}}],
}
```

No `allow_egress` is needed because this command reads the already-cloned workspace. A non-zero
exit and an unparseable line both raise at the point they are known. Do not add an `except` that
returns a default: the linter rejects broad swallowed `ctx` errors because they make an empty run
look successful to both submit verification and callers.
