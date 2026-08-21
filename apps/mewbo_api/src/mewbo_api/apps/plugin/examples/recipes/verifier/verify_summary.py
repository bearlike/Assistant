def verify(result, ctx) -> None:
    """Reject a result whose aggregate cannot reconcile with its rows."""
    input_count = result.get("input_count")
    values = result.get("values")
    total = result.get("total")
    if not isinstance(input_count, int) or input_count < 0:
        raise ValueError("result input_count must be a non-negative integer")
    if not isinstance(values, list) or not all(isinstance(value, int) for value in values):
        raise ValueError("result values must be a list of integers")
    if not isinstance(total, int):
        raise ValueError("result total must be an integer")
    if input_count and not values:
        raise ValueError("non-empty input produced no values")
    if total != sum(values):
        raise ValueError(f"result total {total} does not reconcile with values")
