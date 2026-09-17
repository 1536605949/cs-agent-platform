"""Engine selection.

``ENGINE_MODE=builtin`` (default) uses the self-contained engine.
``ENGINE_MODE=dify`` delegates orchestration to an external Dify application.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from .dify_adapter import DifyEngine
from .engine import Engine

_engine: Engine | DifyEngine | None = None
_engine_mode: str | None = None


def build_engine(settings: Settings | None = None) -> Engine | DifyEngine:
    settings = settings or get_settings()
    if settings.engine_mode == "dify":
        return DifyEngine(settings)
    return Engine(settings)


def get_engine(settings: Settings | None = None) -> Engine | DifyEngine:
    """Return the process-wide engine instance, rebuilding it if the mode changed."""
    global _engine, _engine_mode

    settings = settings or get_settings()
    if _engine is None or _engine_mode != settings.engine_mode:
        _engine = build_engine(settings)
        _engine_mode = settings.engine_mode
    return _engine


def reset_engine() -> None:
    global _engine, _engine_mode
    _engine = None
    _engine_mode = None


__all__ = ["build_engine", "get_engine", "reset_engine"]
