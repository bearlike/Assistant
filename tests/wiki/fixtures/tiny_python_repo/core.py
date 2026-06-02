"""Core engine module."""


class Engine:
    """Drives the main loop."""

    def start(self):
        """Begin execution."""
        return helper()

    def stop(self):
        return False


def run_engine():
    """Entry-point for the engine."""
    e = Engine()
    return e.start()


def helper():
    return 42
