_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "urgency": {"type": "string", "enum": ["low", "high"]},
        "summary": {"type": "string", "minLength": 1},
    },
    "required": ["urgency", "summary"],
    "additionalProperties": False,
}


def triage_prompt(text: str) -> str:
    """Keep prompt construction separate from the model-facing I/O edge."""
    if not text.strip():
        raise ValueError("text must not be empty")
    return f"Classify urgency and summarize this note.\n\n{text}"


def run(params: dict, ctx) -> dict:
    result = ctx.llm(triage_prompt(params["text"]), _TRIAGE_SCHEMA, max_tokens=300)
    return {"urgency": result["urgency"], "summary": result["summary"]}
