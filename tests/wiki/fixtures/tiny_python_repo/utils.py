"""Utility helpers."""
from core import Engine


def normalize(s: str) -> str:
    """Strip whitespace and lowercase."""
    return s.strip().lower()


class StringUtil(Engine):
    """A string-flavored Engine subclass."""

    def fmt(self, s):
        return normalize(s)
