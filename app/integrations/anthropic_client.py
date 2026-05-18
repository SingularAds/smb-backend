"""Anthropic Claude Client"""

from anthropic import AsyncAnthropic
from app.config import settings
import json


class AnthropicClient:
    """Client for Anthropic Claude API"""
    
    def __init__(self):
        self.client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = "claude-sonnet-4-20250514"
    
    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 2000
    ) -> str:
        """Send chat completion request"""
        
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages
        )
        
        return response.content[0].text
    
    async def extract_customer_traits(
        self,
        transcript: list[dict]
    ) -> dict:
        """Extract customer traits from conversation"""
        
        system_prompt = """You are analyzing customer conversations to extract behavioral traits.
        
Extract:
- Scheduling preferences (preferred days, times, frequency)
- Service preferences  
- Communication style
- Personal details relevant to business

Return structured JSON with extracted traits."""
        
        transcript_text = "\n".join([
            f"{msg['role']}: {msg['text']}" 
            for msg in transcript
        ])
        
        response = await self.chat(
            messages=[{
                "role": "user",
                "content": f"Analyze this conversation:\n\n{transcript_text}"
            }],
            system=system_prompt
        )
        
        return json.loads(response)
