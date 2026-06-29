"""Per-customer WhatsApp message debouncer.

Problem
-------
WhatsApp users often send a burst of short messages in rapid succession —
"hello", "how are you", "what is the price?" — each arriving as a separate
webhook event. Without debouncing, the LLM is called once per message, so
the customer receives multiple fragmented replies (one for each message)
instead of a single coherent response.

Solution
--------
Buffer incoming messages for a short window (default 2 s). Every new
message from the same customer resets the timer. When the window expires
with no new messages, all buffered texts are concatenated and the LLM is
called exactly once with the combined input.

Design properties
-----------------
* **Zero infrastructure**: in-memory asyncio tasks — no Redis, no queue.
* **Per-conversation isolation**: keyed by ``"{device_id}:{phone}"``,
  so concurrent bursts from different customers never interfere.
* **Bounded buffer**: at most ``_MAX_BURST`` messages per window to prevent
  memory abuse from a very chatty client.
* **Fire-and-forget safety**: handler exceptions are caught and logged so
  a failing LLM call never corrupts the debounce state for future messages.
* **Single-message transparency**: a burst of one message is passed through
  unchanged after the window expires; no extra latency is added beyond the
  debounce wait itself.
* **Restart caveat**: in-flight buffers are lost on server restart (rare,
  and acceptable — the customer simply sends again).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_MAX_BURST = 10  # max messages to combine per window

# Module-level state — lives for the duration of the server process.
# Keys: "{device_id}:{phone}"
_buffers: dict[str, list[str]] = {}
_tasks: dict[str, asyncio.Task[None]] = {}


async def debounce_message(
    *,
    key: str,
    body: str,
    handler: Callable[[str], Awaitable[None]],
    debounce_s: float,
) -> None:
    """Accept one message for ``key`` and reset the debounce window.

    Args:
        key:        Unique conversation key, e.g. ``"{device_id}:{phone}"``.
        body:       Raw message text just received.
        handler:    Async callable that receives the combined text once the
                    window expires. Called exactly once per burst.
        debounce_s: Seconds of silence before the window is flushed.
    """
    # Append to buffer (capped to prevent runaway growth).
    buf = _buffers.setdefault(key, [])
    buf.append(body)
    if len(buf) > _MAX_BURST:
        del buf[: len(buf) - _MAX_BURST]

    # Cancel any pending flush for this key.
    old = _tasks.get(key)
    if old and not old.done():
        old.cancel()

    async def _flush() -> None:
        await asyncio.sleep(debounce_s)

        messages = _buffers.pop(key, [])
        _tasks.pop(key, None)

        if not messages:
            return

        combined = "\n".join(messages)

        if len(messages) > 1:
            logger.info(
                "[DEBOUNCE] Flushing %d-message burst for key=%r — combined: %r",
                len(messages),
                key,
                combined[:120],
            )

        try:
            await handler(combined)
        except Exception:
            logger.exception("[DEBOUNCE] Handler raised for key=%r", key)

    _tasks[key] = asyncio.create_task(_flush())
