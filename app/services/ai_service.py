"""AI Service — Claude-powered intelligence for onboarding.

Handles language detection, website scraping + extraction,
and free-text service parsing.
"""

import json
import logging
import re

import httpx
from anthropic import AsyncAnthropic

from app.config import settings

logger = logging.getLogger(__name__)

# Phone-prefix → language mapping (longest prefix first)
_PHONE_LANG_MAP: list[tuple[str, str]] = [
    ("351", "pt"),   # Portugal
    ("55",  "pt"),   # Brazil
    ("34",  "es"),   # Spain
    ("33",  "fr"),   # France
    ("972", "en"),   # Israel
    ("44",  "en"),   # UK
    ("49",  "de"),   # Germany
    ("39",  "it"),   # Italy
    ("91",  "en"),   # India
    ("1",   "en"),   # US / Canada
]


def _strip_code_fences(text: str) -> str:
    """Remove Markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


class AIService:
    """Wraps Claude API calls used during onboarding."""

    def __init__(self):
        self.client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = "claude-sonnet-4-20250514"

    # ── language detection ────────────────────────────────────────────────

    @staticmethod
    def detect_language(phone: str) -> str:
        """Infer language from a phone number's country prefix."""
        clean = phone.lstrip("+").replace(" ", "")
        for prefix, lang in sorted(_PHONE_LANG_MAP, key=lambda x: -len(x[0])):
            if clean.startswith(prefix):
                return lang
        return "en"

    # ── website scraping ──────────────────────────────────────────────────

    async def scrape_website(self, url: str) -> dict:
        """Fetch *url*, send the HTML to Claude, return structured biz info.

        Returns an empty dict on any error (caller falls back to manual entry).
        """
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RecepteBot/1.0)"},
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text[:50_000]  # keep payload under Claude limits
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)
            return {}

        extraction_prompt = (
            "Extract business information from this website HTML.\n"
            "Return a JSON object with these fields:\n"
            "{\n"
            '  "name": "business name",\n'
            '  "businessType": "salon|restaurant|clinic|gym|store|other",\n'
            '  "description": "one-sentence description of the business",\n'
            '  "services": [{"name": "...", "duration": "...", "price": "..."}],\n'
            '  "hours": "operating hours as text",\n'
            '  "address": "full address",\n'
            '  "phone": "phone number",\n'
            '  "staff": ["name1", "name2"],\n'
            '  "languages": ["pt", "en"]\n'
            "}\n"
            "Only include fields you can confidently extract. "
            "Use empty string for uncertain fields.\n"
            "For services, include names, durations, and prices where available.\n\n"
            f"HTML:\n{html}"
        )

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                system="You are a data extraction assistant. Return only valid JSON.",
                messages=[{"role": "user", "content": extraction_prompt}],
            )
            raw = _strip_code_fences(response.content[0].text)
            return json.loads(raw)
        except Exception as exc:
            logger.error("Claude extraction failed for %s: %s", url, exc)
            return {}

    # ── service text parsing ──────────────────────────────────────────────

    async def parse_services_text(self, text: str, language: str) -> list[dict]:
        """Parse user-supplied service descriptions into structured objects."""
        prompt = (
            f"Parse these business services into a JSON array.\n"
            f"Each item must have: name, duration (if mentioned), price (if mentioned).\n\n"
            f"User input ({language}):\n{text}\n\n"
            "Return only a JSON array like:\n"
            '[{"name": "...", "duration": "...", "price": "..."}]'
        )

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=1000,
                system="Parse service listings into JSON. Return only a valid JSON array.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = _strip_code_fences(response.content[0].text)
            return json.loads(raw)
        except Exception as exc:
            logger.error("Service parsing failed: %s", exc)
            return []
