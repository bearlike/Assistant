"""Entry point."""
import core
from utils import normalize


def main():
    """Run the application."""
    result = core.run_engine()
    return normalize(str(result))
