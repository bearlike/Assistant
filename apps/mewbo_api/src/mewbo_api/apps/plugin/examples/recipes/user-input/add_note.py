import hashlib


def note_key(text: str) -> str:
    """Use content identity so identical form submissions remain one document."""
    normalized = text.strip()
    if not normalized:
        raise ValueError("note text must not be blank")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def run(params: dict, ctx) -> dict:
    text = params["text"]
    key = note_key(text)
    ctx.collection("notes").upsert(
        key,
        {
            "text": text.strip(),
            "pinned": params.get("pinned", False),
        },
    )
    return {"saved": key}
