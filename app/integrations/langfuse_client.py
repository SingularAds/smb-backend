"""Langfuse LLM observability — thin wrapper over the SDK.

Drop-in OpenAI replacement: import ``from app.integrations.langfuse_client
import get_async_openai`` and every chat completion is auto-traced
(prompt, response, tokens, cost, latency).

We expose a single ``get_async_openai()`` factory so the rest of the
codebase doesn't need to know whether Langfuse is enabled — when
``LANGFUSE_ENABLED`` is False or keys are missing, the factory returns a
vanilla ``openai.AsyncOpenAI`` and tracing is silently disabled.

Trace metadata is set per-call via the ``metadata`` kwarg supported by
the langfuse-wrapped client (business_id, customer_phone, intent, etc.)
so dashboards can filter conversations by business and link CSAT scores
back to the exact AI turn.
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_langfuse_singleton: Any = None
_initialised: bool = False


def _enabled() -> bool:
    return bool(
        settings.LANGFUSE_ENABLED
        and settings.LANGFUSE_PUBLIC_KEY
        and settings.LANGFUSE_SECRET_KEY
    )


def get_langfuse() -> Any:
    """Return the singleton Langfuse client, or None when disabled.

    Safe to call from request handlers — initialisation is cached.
    """
    global _langfuse_singleton, _initialised
    if _initialised:
        return _langfuse_singleton
    _initialised = True
    if not _enabled():
        logger.info("[LANGFUSE] disabled (keys not set or LANGFUSE_ENABLED=False)")
        return None
    try:
        from langfuse import Langfuse  # type: ignore
        _langfuse_singleton = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
        )
        logger.info("[LANGFUSE] enabled host=%s", settings.LANGFUSE_HOST)
    except Exception as exc:
        logger.warning("[LANGFUSE] failed to initialise — tracing disabled: %s", exc)
        _langfuse_singleton = None
    return _langfuse_singleton


def get_async_openai(api_key: str) -> Any:
    """Return an AsyncOpenAI client.

    When Langfuse is enabled and import succeeds, returns the langfuse-
    wrapped client so every ``client.chat.completions.create(...)`` call
    is automatically traced. Otherwise returns the vanilla SDK client.
    """
    if _enabled():
        try:
            # langfuse 2.x reads credentials from module-level attributes on
            # the `openai` package — without these, _wrap_async raises
            # AttributeError: module 'openai' has no attribute
            # 'langfuse_public_key'. Set them BEFORE importing
            # langfuse.openai so the monkey-patch sees them.
            import openai as _openai_mod
            _openai_mod.langfuse_public_key = settings.LANGFUSE_PUBLIC_KEY
            _openai_mod.langfuse_secret_key = settings.LANGFUSE_SECRET_KEY
            _openai_mod.langfuse_host = settings.LANGFUSE_HOST

            # The langfuse package ships a drop-in `openai` module that
            # exposes AsyncOpenAI with the same interface plus tracing.
            from langfuse.openai import AsyncOpenAI as TracedAsyncOpenAI  # type: ignore
            # Ensure the singleton is created so config is registered
            get_langfuse()
            return TracedAsyncOpenAI(api_key=api_key)
        except Exception as exc:
            logger.warning(
                "[LANGFUSE] could not load langfuse.openai wrapper — "
                "falling back to vanilla OpenAI: %s", exc,
            )
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=api_key)


def score_trace(
    trace_id: str,
    name: str,
    value: float,
    comment: str = "",
) -> None:
    """Attach an evaluation score to a previous trace.

    Used by the CSAT flow to link a 1-5 rating to the AI conversation that
    earned it — so in Langfuse you can filter all traces with rating <= 2
    and see exactly what the AI said wrong.
    """
    client = get_langfuse()
    if client is None or not trace_id:
        return
    try:
        client.score(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment or None,
        )
    except Exception as exc:
        logger.warning("[LANGFUSE] could not record score %s=%s: %s", name, value, exc)
