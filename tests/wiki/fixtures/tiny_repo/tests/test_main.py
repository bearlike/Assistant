"""Placeholder tests for tiny_repo."""
from src.utils import greet


def test_greet() -> None:
    assert greet("world") == "Hello, world!"
