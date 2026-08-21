# Transform with an LLM step

Use a code pipeline plus `ctx.llm` for the small part that needs model output; keep prompt
construction and the rest deterministic. The runner validates `output_schema`, retries one rejected
model response, then raises if it still cannot produce the schema. Do not catch that error and invent
a fallback classification: an unknown classification must fail visibly.

```python
{
    "name": "triage-note",
    "wake_prompt": "Classify a submitted note for urgency.",
    "mode": "code",
    "tier": "render",
    "entrypoint": "pipelines/triage.py",
    "on_demand": True,
    "params_schema": {
        "type": "object",
        "properties": {"text": {"type": "string", "minLength": 1}},
        "required": ["text"],
        "additionalProperties": False,
    },
    "llm_budget_tokens": 300,
    "timeout_seconds": 120,
    "result": {
        "media": "json",
        "json_schema": {
            "type": "object",
            "properties": {
                "urgency": {"type": "string", "enum": ["low", "high"]},
                "summary": {"type": "string", "minLength": 1},
            },
            "required": ["urgency", "summary"],
            "additionalProperties": False,
        },
    },
    "samples": [{"label": "short note", "params": {"text": "Review the release notes."}}],
}
```

The declared budget must cover every `max_tokens` request in one run. `samples` make submit replay
real parameters; without them a required-parameter pipeline receives only the weaker empty-params
check. The pipeline linter also refuses the sloppy broad-catch shape around guarded `ctx` I/O.
