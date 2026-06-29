"""Internal message types used across the web-assistant pipeline.

These dataclasses form the contract between the Chatwoot adapter, the
orchestrator service, and the transport layer. Keeping them small and
provider-agnostic means a future channel (Instagram DM, native web chat,
etc.) only needs a new adapter — the rest of the pipeline is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SenderType(str, Enum):
    """Who sent the inbound message.

    visitor — a website visitor (Chatwoot ``Contact``)
    agent   — a human agent on the Recepte team (Chatwoot ``User``)
    bot     — an automated message (Chatwoot ``AgentBot``); we ignore these
    system  — Chatwoot system events; we ignore these
    """

    VISITOR = "visitor"
    AGENT = "agent"
    BOT = "bot"
    SYSTEM = "system"


class WebhookEvent(str, Enum):
    """Chatwoot webhook event types we react to."""

    CONVERSATION_CREATED = "conversation_created"
    MESSAGE_CREATED = "message_created"


@dataclass
class InboundChatMessage:
    """Normalised inbound event from Chatwoot.

    Built by ``adapter.parse_event``; consumed by ``service.handle_event``.
    The ``raw`` field is kept only for debugging / structured logging — no
    business logic should peek inside it.
    """

    event: WebhookEvent
    account_id: str
    inbox_id: int
    conversation_id: str
    message_id: str | None
    sender_type: SenderType
    sender_name: str | None
    body: str
    is_private_note: bool
    visitor_browser_language: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_command(self) -> bool:
        """True when an agent posted a private note that starts with ``/``."""
        if not (self.sender_type is SenderType.AGENT and self.is_private_note):
            return False
        cleaned = self.body.strip().strip("*_` ")
        return cleaned.startswith("/")


@dataclass
class OutboundChatMessage:
    """A reply we want Chatwoot to deliver back to the visitor."""

    conversation_id: str
    text: str
    private: bool = False  # True → only agents see it (internal note)
