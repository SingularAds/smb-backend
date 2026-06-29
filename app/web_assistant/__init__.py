"""Website AI Assistant (Chatwoot channel).

A self-contained pipeline that answers questions on the recepte.co chat
widget using the Global Recepte KB plus human-approved learnings.

Hard isolation guarantee:
  * Does not import or call any WhatsApp module
  * Does not write to any WhatsApp collection
  * Does not reuse the WhatsApp system prompt
  * Reuses only the OpenAI adapter, Langfuse client, PostHog client, and
    the read-only `services.global_kb.get_global_kb()` helper.

Module map:
  webhook.py   — POST /webhook/chatwoot entrypoint (HMAC verify + dispatch)
  adapter.py   — Chatwoot payload → InboundChatMessage
  types.py     — Internal dataclasses spoken by every layer
  service.py   — Orchestrator: history + KB + learnings + LLM → reply or escalate
  prompt.py    — Website system prompt template (no WhatsApp references)
  transport.py — Chatwoot REST client (send message, private note, assign team)
  store.py     — Firestore I/O for web_conversations
  learnings.py — Firestore I/O + in-process cache for approved Q&A learnings
  commands.py  — Detect agent commands (/kb-learn) in private notes
"""
