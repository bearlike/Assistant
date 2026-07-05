#!/usr/bin/env python3
"""ContextMeter — tokens → context-window % and tokens → cost (issue #156).

One atomic class that turns a live token count into the two honest numbers the
status bar shows:

- **context-window %** — ``used / total``. ``total`` comes from the configured
  ``llm.model_context_windows`` map (exact, by model name), else the configured
  ``llm.default_context_window`` (an *estimate* — flagged with a ``~`` prefix so
  the user knows it is not the real window for this model).
- **cost ($)** — derived from input/output tokens via LiteLLM's per-token price
  table (the same pricing the proxy bills on). When LiteLLM has no price for the
  model we degrade *honestly* to ``None`` (the status bar renders ``—``) rather
  than inventing a number.

KISS/DRY: pricing comes from LiteLLM (already a project dependency for every LLM
call), the window map comes from the existing config accessor. No bespoke
pricing table, no hardcoded numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from mewbo_core.config import get_config_value


@dataclass(frozen=True)
class ContextUsage:
    """The computed context-window usage for one render.

    ``estimated`` is ``True`` when ``total`` is the configured default rather
    than a model-specific window — the status bar prefixes ``~`` in that case.
    """

    used: int
    total: int
    percent: float
    estimated: bool

    @property
    def over_warning(self) -> bool:
        """Whether usage has crossed the 80% warning threshold."""
        return self.percent >= ContextMeter.WARNING_PERCENT


class ContextMeter:
    """Compute context-window % and session cost from token counts.

    Reads ``llm.model_context_windows`` / ``llm.default_context_window`` once per
    call (cheap getattr walk) so a config change mid-session is honored. Inject
    overrides in tests via the constructor rather than patching config.
    """

    #: Usage at or above this percent is rendered in the warning style.
    WARNING_PERCENT: float = 80.0

    def __init__(
        self,
        *,
        windows: dict[str, int] | None = None,
        default_window: int | None = None,
    ) -> None:
        """Bind optional explicit window overrides (else read live from config)."""
        self._windows = windows
        self._default_window = default_window

    # -- context window ---------------------------------------------------

    def _resolve_window(self, model: str | None) -> tuple[int, bool]:
        """Return ``(total_tokens, estimated)`` for ``model``.

        Exact match in the window map → ``estimated=False``. Otherwise the
        configured default → ``estimated=True``.
        """
        windows = self._windows
        if windows is None:
            windows = dict(get_config_value("llm", "model_context_windows", default={}) or {})
        if model:
            # Exact, then suffix match (config may key by bare name while the
            # runtime model carries a ``openai/`` proxy prefix, or vice-versa).
            if model in windows:
                return int(windows[model]), False
            bare = model.rsplit("/", 1)[-1]
            for key, value in windows.items():
                if key == bare or key.rsplit("/", 1)[-1] == bare:
                    return int(value), False
        default = self._default_window
        if default is None:
            default = int(
                get_config_value("llm", "default_context_window", default=128000) or 128000
            )
        return int(default), True

    def usage(self, *, used_tokens: int, model: str | None) -> ContextUsage:
        """Compute :class:`ContextUsage` for ``used_tokens`` against ``model``.

        ``percent`` is clamped to ``[0, 100]`` and ``0`` when the window is
        non-positive (never divides by zero, never raises).
        """
        total, estimated = self._resolve_window(model)
        used = max(0, int(used_tokens))
        if total <= 0:
            percent = 0.0
        else:
            percent = min(100.0, max(0.0, used / total * 100.0))
        return ContextUsage(used=used, total=total, percent=percent, estimated=estimated)

    # -- cost -------------------------------------------------------------

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        model: str | None,
    ) -> float | None:
        """Return the session cost in USD, or ``None`` when price is unknown.

        Uses LiteLLM's per-token price table. A model LiteLLM does not price (or
        any lookup error) returns ``None`` — the caller renders ``—`` rather
        than a fabricated figure.
        """
        if not model:
            return None
        try:
            import litellm
        except Exception:
            return None
        # Silence LiteLLM's stderr "Provider List" banner on an unknown model —
        # an unpriced model is an expected, handled case here (we return None).
        litellm.suppress_debug_info = True
        candidates = [model, model.rsplit("/", 1)[-1]]
        for name in candidates:
            try:
                prompt_cost, completion_cost = litellm.cost_per_token(
                    model=name,
                    prompt_tokens=max(0, int(input_tokens)),
                    completion_tokens=max(0, int(output_tokens)),
                )
            except Exception:
                continue
            total = float(prompt_cost) + float(completion_cost)
            if total >= 0:
                return total
        return None

    # -- formatting helpers (pure) ----------------------------------------

    @staticmethod
    def format_tokens(count: int) -> str:
        """Format a token count compactly: ``842``, ``12.3K``, ``1.4M``."""
        count = max(0, int(count))
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1_000:
            return f"{count / 1_000:.1f}K"
        return str(count)

    @staticmethod
    def format_cost(cost: float | None) -> str:
        """Format a USD cost honestly: ``—`` when unknown, else ``$0.0123``."""
        if cost is None:
            return "—"
        if cost < 0.01:
            return f"${cost:.4f}"
        return f"${cost:.2f}"


__all__ = ["ContextMeter", "ContextUsage"]
