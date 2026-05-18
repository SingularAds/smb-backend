"""ElevenLabs TTS Client"""

import httpx
from app.config import settings


class ElevenLabsClient:
    """Client for ElevenLabs Text-to-Speech API"""
    
    BASE_URL = "https://api.elevenlabs.io/v1"
    
    # Multilingual voices
    VOICES = {
        "female": {
            "default": "EXAVITQu4vr4xnSDxMaL",  # Sarah
            "pt": "EXAVITQu4vr4xnSDxMaL",
            "en": "EXAVITQu4vr4xnSDxMaL",
            "es": "EXAVITQu4vr4xnSDxMaL",
            "fr": "EXAVITQu4vr4xnSDxMaL",
        }
    }
    
    async def synthesize(self, text: str, lang: str = "en") -> bytes:
        """Generate speech audio from text"""
        
        voice_id = self.VOICES["female"].get(lang, self.VOICES["female"]["default"])
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/text-to-speech/{voice_id}",
                json={
                    "text": text,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                        "style": 0.3,
                        "use_speaker_boost": True
                    }
                },
                headers={
                    "xi-api-key": settings.ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg"
                },
                timeout=10.0
            )
            
            response.raise_for_status()
            return response.content
