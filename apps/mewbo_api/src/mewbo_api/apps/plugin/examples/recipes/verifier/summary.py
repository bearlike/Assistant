def summarize(values: list[int]) -> dict:
    """Construct a result whose relation is checked separately by the verifier."""
    if not all(isinstance(value, int) for value in values):
        raise ValueError("values must contain only integers")
    return {"input_count": len(values), "values": values, "total": sum(values)}


def run(params: dict, ctx) -> dict:
    return summarize(params["values"])
