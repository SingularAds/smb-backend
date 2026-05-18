"""Owner command parser — keyword-based, no AI.

Maps incoming WhatsApp text to a structured command dict:
  {"type": CommandType, "args": dict}

Supports English and Portuguese keywords, case-insensitive.
"""
from __future__ import annotations

import logging
import re
from enum import Enum

logger = logging.getLogger(__name__)


class CommandType(str, Enum):
    # Read / reporting
    TODAY = "today"
    TOMORROW = "tomorrow"
    SUMMARY = "summary"
    VIP = "vip"
    SETTINGS = "settings"

    # Booking management
    CANCEL = "cancel"
    BLOCK = "block"

    # Business config
    ADD_SERVICE = "add_service"
    REMOVE_SERVICE = "remove_service"
    SHOW_SERVICES = "show_services"
    CHANGE_HOURS = "change_hours"
    CLOSE_DAY = "close_day"
    ADD_FAQ = "add_faq"
    ADD_STYLIST = "add_stylist"
    CHANGE_VIBE = "change_vibe"
    SCAN_WEBSITE = "scan_website"

    # Customer comms
    INACTIVE_CLIENTS = "inactive_clients"
    SEND_OUTREACH = "send_outreach"

    # System
    AUTO_REPLY_OFF = "auto_reply_off"
    AUTO_REPLY_ON = "auto_reply_on"

    # Meta
    HELP = "help"
    UNKNOWN = "unknown"


# fmt: off
_PATTERNS: list[tuple[CommandType, list[str]]] = [
    (CommandType.TODAY,          ["hoje", "today", "todays", "today's", "agenda hoje", "today's bookings", "todays bookings", "booking today", "bookings today", "marcações hoje"]),
    (CommandType.TOMORROW,       ["amanhã", "amanha", "tomorrow", "tomorrows", "tomorrow's", "agenda amanhã", "tomorrow's bookings", "tomorrows bookings", "booking tomorrow", "bookings tomorrow"]),
    (CommandType.SUMMARY,        ["resumo", "summary", "relatorio", "relatório", "report", "stats", "estatísticas"]),
    (CommandType.VIP,            ["vip", "clientes vip", "vip clients", "clientes especiais"]),
    (CommandType.SETTINGS,       ["definições", "definicoes", "settings", "configurações", "configuracoes", "config"]),
    (CommandType.CANCEL,         ["cancelar", "cancel", "cancela"]),
    (CommandType.BLOCK,          ["bloquear", "block", "bloqueia", "bloquear horário", "block slot"]),
    (CommandType.SHOW_SERVICES,    ["show services", "list services", "my services", "ver serviços", "listar serviços", "os meus serviços", "listar servicos", "ver servicos"]),
    (CommandType.ADD_SERVICE,      ["adicionar serviço", "adicionar servico", "add service", "novo serviço", "new service", "adiciona serviço"]),
    (CommandType.REMOVE_SERVICE,   ["remover serviço", "remover servico", "remove service", "apagar serviço", "delete service"]),
    (CommandType.CHANGE_HOURS,     ["change hours", "mudar horário", "mudar horario", "alterar horário", "update hours", "horário de funcionamento"]),
    (CommandType.CLOSE_DAY,        ["closed", "close day", "fechado", "fechar dia", "day off", "folga", "não abre", "nao abre",
                                     "open monday", "open tuesday", "open wednesday", "open thursday", "open friday", "open saturday", "open sunday",
                                     "open segunda", "open terca", "open quarta", "open quinta", "open sexta", "open sabado", "open domingo",
                                     "abrir segunda", "abrir sexta", "abrir sabado", "abrir domingo"]),
    (CommandType.ADD_FAQ,          ["add faq", "adicionar faq", "nova pergunta", "new faq", "faq"]),
    (CommandType.ADD_STYLIST,      ["add stylist", "adicionar estilista", "novo estilista", "new stylist", "add staff", "adicionar funcionário"]),
    (CommandType.CHANGE_VIBE,      ["change vibe", "mudar vibe", "vibe to", "vibe casual", "vibe professional", "vibe luxury", "vibe friendly"]),
    (CommandType.SCAN_WEBSITE,     ["scan", "scan website", "add website", "import website", "digitalizar site", "adicionar site"]),
    (CommandType.INACTIVE_CLIENTS, ["inactive clients", "inactive customers", "clientes inativos", "sem visitas", "not visited", "haven't visited", "inactive"]),
    (CommandType.SEND_OUTREACH,    ["send outreach", "outreach", "contact inactive", "message inactive", "enviar mensagem", "contactar inativos"]),
    (CommandType.AUTO_REPLY_OFF,   ["turn off auto reply", "disable auto reply", "stop auto reply", "desativar resposta", "parar resposta automática", "auto reply off"]),
    (CommandType.AUTO_REPLY_ON,    ["turn on auto reply", "enable auto reply", "start auto reply", "ativar resposta", "auto reply on"]),
    (CommandType.HELP,             ["ajuda", "help", "comandos", "commands", "menu", "opções", "opcoes"]),
]
# fmt: on


def parse_command(message: str) -> dict:
    """Return {"type": CommandType, "args": dict, "raw": str}.

    Matching strategy (in order):
    1. Exact phrase/keyword present anywhere in the message (word-boundary aware)
    2. Any keyword is a substring of the message (catches typos / loose phrasing)
    """
    normalised = message.strip().lower()
    logger.debug("[PARSER] input=%r", normalised)

    # Pass 1 — word-boundary match
    for cmd_type, keywords in _PATTERNS:
        for kw in keywords:
            pattern = r"(?<!\w)" + re.escape(kw) + r"(?!\w)"
            if re.search(pattern, normalised):
                args = _extract_args(cmd_type, message)
                logger.debug("[PARSER] matched (boundary) cmd=%s kw=%r", cmd_type, kw)
                return {"type": cmd_type, "args": args, "raw": message}

    # Pass 2 — substring match (handles todays / booking today / loose input)
    for cmd_type, keywords in _PATTERNS:
        for kw in keywords:
            if kw in normalised:
                args = _extract_args(cmd_type, message)
                logger.debug("[PARSER] matched (substring) cmd=%s kw=%r", cmd_type, kw)
                return {"type": cmd_type, "args": args, "raw": message}

    logger.debug("[PARSER] no match → UNKNOWN")
    return {"type": CommandType.UNKNOWN, "args": {}, "raw": message}


# ── argument extractors ───────────────────────────────────────────────────────

def _extract_args(cmd_type: CommandType, message: str) -> dict:
    if cmd_type == CommandType.CANCEL:
        m = re.search(r"(?:cancelar|cancel)\s+(\S+)", message, re.IGNORECASE)
        if m:
            ref = m.group(1)
            # Only treat as a real ref if it contains a digit (phone/booking ID)
            # Plain words like "booking", "marcação", "all" are NOT refs
            if any(ch.isdigit() for ch in ref):
                return {"ref": ref}
        return {"ref": None}

    if cmd_type == CommandType.BLOCK:
        m = re.search(r"(?:bloquear|block)\s+(.+)", message, re.IGNORECASE)
        return {"slot": m.group(1).strip() if m else None}

    if cmd_type == CommandType.ADD_SERVICE:
        # Accept "add service Name | 45 | 20" or multi-line with pipe separators
        m = re.search(r"(?:add service|adicionar servi[cç]o|novo servi[cç]o|new service|adiciona servi[cç]o)\s*[:\n]?\s*(.+)", message, re.IGNORECASE | re.DOTALL)
        if m:
            detail = m.group(1).strip()
            if "|" in detail:
                parts = [p.strip() for p in detail.split("|")]
                return {"detail": detail, "name": parts[0], "duration": parts[1] if len(parts) > 1 else None, "price": parts[2] if len(parts) > 2 else None}
        return {"detail": None}

    if cmd_type == CommandType.REMOVE_SERVICE:
        m = re.search(r"(?:remove service|remover servi[cç]o|apagar servi[cç]o|delete service)\s+(.+)", message, re.IGNORECASE)
        return {"ref": m.group(1).strip() if m else None}

    if cmd_type == CommandType.CHANGE_HOURS:
        # "change hours Mon-Sat 9-19" or "change hours Monday 9:00-18:00" or "change hours Monday closed"
        m = re.search(
            r"(?:change hours|mudar hor[aá]rio|alterar hor[aá]rio|update hours)\s+(.+)",
            message, re.IGNORECASE
        )
        return {"spec": m.group(1).strip() if m else None}

    if cmd_type == CommandType.CLOSE_DAY:
        # "closed friday", "fechado sexta", "closed 2026-04-25", "open friday"
        m_open = re.search(r"\b(?:open|abrir)\b\s+(.+)", message, re.IGNORECASE)
        if m_open:
            return {"spec": m_open.group(1).strip(), "open": True}
        m = re.search(r"(?:closed|close day|fechado|fechar dia|day off|folga|n[aã]o abre)\s+(.+)", message, re.IGNORECASE)
        return {"spec": m.group(1).strip() if m else None, "open": False}

    if cmd_type == CommandType.ADD_FAQ:
        # "add faq: question | answer" or "add faq question | answer"
        m = re.search(r"(?:add faq|adicionar faq|nova pergunta|new faq|faq)[:\s]+(.+)", message, re.IGNORECASE | re.DOTALL)
        if m:
            raw = m.group(1).strip()
            # Split on | or newline
            sep = re.split(r"\||¿|\n", raw, maxsplit=1)
            if len(sep) == 2:
                return {"question": sep[0].strip(), "answer": sep[1].strip()}
            return {"raw": raw, "question": raw, "answer": None}
        return {"question": None, "answer": None}

    if cmd_type == CommandType.ADD_STYLIST:
        # "add stylist Maria, specialties: color" or "add stylist Ana | 351912345678"
        m = re.search(r"(?:add stylist|adicionar estilista|novo estilista|new stylist|add staff|adicionar funcion[aá]rio)\s+(.+)", message, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            # Parse specialties
            spec_match = re.search(r"(?:specialties?|especialidades?)[:\s]+(.+)", raw, re.IGNORECASE)
            specialties = spec_match.group(1).strip() if spec_match else None
            # Name = everything before comma/pipe/specialties keyword
            name_raw = re.split(r",|\|", raw)[0].strip()
            name_raw = re.sub(r"(?:specialties?|especialidades?).*", "", name_raw, flags=re.IGNORECASE).strip()
            return {"name": name_raw or raw, "specialties": specialties}
        return {"name": None, "specialties": None}

    if cmd_type == CommandType.CHANGE_VIBE:
        # "change vibe to casual", "vibe professional"
        m = re.search(
            r"(?:change vibe|mudar vibe|vibe to|vibe)[:\s]+(.+)",
            message, re.IGNORECASE
        )
        if not m:
            m = re.search(r"vibe\s+(\S+)", message, re.IGNORECASE)
        if m:
            vibe = re.sub(r"^(?:to|para)\s+", "", m.group(1).strip(), flags=re.IGNORECASE).lower()
            return {"vibe": vibe}
        return {"vibe": None}

    if cmd_type == CommandType.SCAN_WEBSITE:
        # "scan mysite.com", "scan https://mysite.com"
        m = re.search(r"(?:scan|add website|import website|digitalizar site|adicionar site)\s+(\S+)", message, re.IGNORECASE)
        url = m.group(1).strip() if m else None
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        return {"url": url}

    if cmd_type == CommandType.INACTIVE_CLIENTS:
        # "inactive clients 60" or "inactive 30 days"
        m = re.search(r"(\d+)", message)
        days = int(m.group(1)) if m else 30
        return {"days": days}

    if cmd_type == CommandType.SEND_OUTREACH:
        # "send outreach Hi come back!"  — everything after the keyword
        m = re.search(r"(?:send outreach|outreach|contact inactive|message inactive|enviar mensagem|contactar inativos)[:\s]+(.+)", message, re.IGNORECASE | re.DOTALL)
        return {"message": m.group(1).strip() if m else None}

    return {}
