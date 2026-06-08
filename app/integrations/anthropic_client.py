"""Anthropic Claude Client"""

from app.integrations.openai_adapter import AsyncOpenAIAnthropicWrapper
from app.config import settings
import json


class AnthropicClient:
    """Client for Anthropic Claude API (Now routed to OpenAI for backward compatibility)"""
    
    def __init__(self):
        self.client = AsyncOpenAIAnthropicWrapper(api_key=settings.OPENAI_API_KEY)
        self.model = "gpt-4o-mini"
    
    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 2000
    ) -> str:
        """Send chat completion request"""
        
        import time
        import logging
        start_time = time.time()
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages
        )
        process_time = time.time() - start_time
        logging.info(f"[LATENCY] AI Model Request ({self.model}) took {process_time:.3f}s")
        
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
