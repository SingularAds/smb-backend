"""Agent slash-commands posted as Chatwoot private notes.

Recognised commands (configurable via settings.CHATWOOT_*_COMMAND):
  * ``/kb-learn``   — save the latest visitor question + agent reply to the KB
  * ``/ai-pause``   — stop the AI from replying to this conversation
  * ``/ai-resume``  — re-enable AI replies for this conversation
  * ``/ai-help``    — list the available commands as a private note

Each command is dispatched by service._handle_agent_message via the
``Command`` enum so that adding a new command is a single-line change.

A learning is recorded by:
  1. Preferring ``doc['lastEscalation'].question`` (set on KB-miss escalations)
     so explicit escalations capture the exact question the AI gave up on.
  2. Falling back to the most recent visitor message in ``doc['messages']``
     so corrections to the AI's answers can also be saved.
  3. Pulling the most recent assistant public reply from ``doc['messages']``
     — that is the human agent's reply if they replied publicly.
  4. Persisting Q+A via ``learnings.save_learning`` (dedup'd by hash).
  5. Confirming success/failure with a private note back to the agent.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any

from app.config import settings
from app.web_assistant import learnings as web_learnings
from app.web_assistant import prompt as prompt_mod
from app.web_assistant import store, transport

logger = logging.getLogger(__name__)


class Command(str, Enum):
    LEARN = "learn"
    PAUSE = "pause"
    RESUME = "resume"
    HELP = "help"


_COMMAND_PATTERN = re.compile(r"^\s*(/[a-zA-Z][-a-zA-Z0-9_]*)\b")


def parse(text: str) -> Command | None:
    """Return the recognised :class:`Command`, or None when text is not a command."""
    if not text:
        return None
    cleaned = text.strip().strip("*_` ")
    match = _COMMAND_PATTERN.match(cleaned)
    print(f"[WEB-AI] parse() cleaned={cleaned!r} match={match}")
    if not match:
        return None
    token = match.group(1).lower()

    if token == settings.CHATWOOT_LEARN_COMMAND.lower():
        return Command.LEARN
    if token == settings.CHATWOOT_PAUSE_COMMAND.lower():
        return Command.PAUSE
    if token == settings.CHATWOOT_RESUME_COMMAND.lower():
        return Command.RESUME
    if token == settings.CHATWOOT_HELP_COMMAND.lower():
        return Command.HELP

    logger.info("[WEB-AI] Unknown agent command: %s", token)
    return None


# ── /kb-learn ────────────────────────────────────────────────────────────────


def _extract_learn_id(body: str) -> str | None:
    """Extract the hex escalation ID from '/kb-learn <id>', or return None."""
    cleaned = (body or "").strip().strip("*_` ")
    print(f"[WEB-AI] _extract_learn_id() cleaned={cleaned!r}")
    parts = cleaned.split(None, 1)
    if len(parts) < 2:
        return None
    candidate = parts[1].strip().strip("*_` ")
    print(f"[WEB-AI] _extract_learn_id() candidate={candidate!r}")
    if re.match(r"^[0-9a-fA-F]{8,32}$", candidate):
        return candidate.lower()
    return None


async def execute_learn(
    *,
    conversation_id: str,
    doc: dict[str, Any],
    agent_name: str | None,
    body: str = "",
) -> None:
    """Save (question + last assistant reply) as a learning, then confirm.

    Two modes:
      * ``/kb-learn <id>`` — saves the escalation pinned to that ID (supports
        multiple concurrent unanswered questions in one conversation).
      * ``/kb-learn``      — saves the most recent visitor question (fallback,
        works when there was no explicit escalation).
    """
    esc_id = _extract_learn_id(body)

    if esc_id:
        pending = store.pop_pending_escalation(doc, esc_id)
        if pending is None:
            await _note(conversation_id, doc, prompt_mod.learn_failure_note(
                f"no pending escalation found with ID **{esc_id}** — "
                "it may have already been saved"
            ))
            return
        question = (pending.get("question") or "").strip()
    else:
        question = (store.last_visitor_question(doc) or "").strip()

    if not question:
        await _note(conversation_id, doc, prompt_mod.learn_failure_note(
            "no visitor question on record for this conversation"
        ))
        return

    answer = (store.last_assistant_public_reply(doc) or "").strip()
    if not answer:
        await _note(conversation_id, doc, prompt_mod.learn_failure_note(
            "no human answer yet — reply to the visitor first, then post the command"
        ))
        return

    language = doc.get("language") or "en"
    if answer.strip() == prompt_mod.escalation_public_message(language).strip():
        await _note(conversation_id, doc, prompt_mod.learn_failure_note(
            "the last assistant message is still my hand-off greeting — "
            "please reply to the visitor first"
        ))
        return

    try:
        web_learnings.save_learning(
            question=question,
            answer=answer,
            agent_name=agent_name,
            conversation_id=conversation_id,
            language=language,
        )
    except Exception as exc:
        logger.exception("[WEB-AI] save_learning failed: %s", exc)
        await _note(conversation_id, doc, prompt_mod.learn_failure_note(
            f"internal error ({type(exc).__name__})"
        ))
        return

    await _note(conversation_id, doc, prompt_mod.learn_success_note(question, esc_id))


# ── /ai-pause ────────────────────────────────────────────────────────────────


async def execute_pause(
    *,
    conversation_id: str,
    doc: dict[str, Any],
) -> None:
    """Pause AI replies for this conversation. Idempotent."""
    if doc.get("aiPaused"):
        await _note(conversation_id, doc, prompt_mod.pause_already_note())
        return

    store.mark_ai_paused(doc, True)
    logger.info("[WEB-AI] AI paused via command for conv=%s", conversation_id)
    await _note(
        conversation_id,
        doc,
        prompt_mod.pause_success_note(settings.CHATWOOT_RESUME_COMMAND),
    )


# ── /ai-resume ───────────────────────────────────────────────────────────────


async def execute_resume(
    *,
    conversation_id: str,
    doc: dict[str, Any],
) -> None:
    """Re-enable AI replies for this conversation. Idempotent."""
    if not doc.get("aiPaused"):
        await _note(conversation_id, doc, prompt_mod.resume_already_note())
        return

    store.mark_ai_paused(doc, False)
    logger.info("[WEB-AI] AI resumed via command for conv=%s", conversation_id)
    await _note(conversation_id, doc, prompt_mod.resume_success_note())


# ── /ai-help ─────────────────────────────────────────────────────────────────


async def execute_help(
    *,
    conversation_id: str,
    doc: dict[str, Any],
) -> None:
    """List available agent commands as a private note."""
    await _note(
        conversation_id,
        doc,
        prompt_mod.help_note(
            learn_command=settings.CHATWOOT_LEARN_COMMAND,
            pause_command=settings.CHATWOOT_PAUSE_COMMAND,
            resume_command=settings.CHATWOOT_RESUME_COMMAND,
        ),
    )


# ── Internal -----------------------------------------------------------------


async def _note(conversation_id: str, doc: dict[str, Any], text: str) -> None:
    """Send a private note and register its ID for echo suppression."""
    note_id = await transport.send_private_note(conversation_id, text)
    if note_id:
        store.register_bot_message(doc, note_id)
        store.save(conversation_id, doc)
