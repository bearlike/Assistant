# Semantic verifier

A result schema checks fields independently; a verifier checks relationships between them.
`verify_summary.py` rejects a total that does not reconcile with the values and a result that loses
all values despite a non-empty input. It reports failure only by raising.

```python
{
    "name": "value-summary",
    "wake_prompt": "Return a checked summary of submitted integer values.",
    "mode": "code",
    "tier": "render",
    "entrypoint": "pipelines/summary.py",
    "on_demand": True,
    "params_schema": {
        "type": "object",
        "properties": {"values": {"type": "array", "items": {"type": "integer"}}},
        "required": ["values"],
        "additionalProperties": False,
    },
    "result": {
        "media": "json",
        "json_schema": {
            "type": "object",
            "properties": {
                "input_count": {"type": "integer", "minimum": 0},
                "values": {"type": "array", "items": {"type": "integer"}},
                "total": {"type": "integer"},
            },
            "required": ["input_count", "values", "total"],
            "additionalProperties": False,
        },
    },
    "verifier": {
        "entrypoint": "pipelines/verify_summary.py",
        "timeout_seconds": 30,
        "consecutive_failures_to_invalidate": 2,
    },
    "samples": [{"label": "values reconcile", "params": {"values": [3, 5, 8]}}],
}
```

Keep the pipeline's pure calculation and the verifier's pure semantic check separate. Neither
should suppress errors: an exception is the platform's signal to reject a bad submit or flag a
returned result rather than falsely certify it.
