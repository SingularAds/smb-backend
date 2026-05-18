"""Voice Service - Handles Twilio voice calls"""

from twilio.twiml.voice_response import VoiceResponse, Gather
from datetime import datetime
import uuid

from app.integrations.anthropic_client import AnthropicClient
from app.integrations.elevenlabs_client import ElevenLabsClient
from app.config import settings


class VoiceService:
    """Service for handling voice calls via Twilio"""
    
    def __init__(self):
        self.anthropic_client = AnthropicClient()
        self.tts_client = ElevenLabsClient()
        self.base_url = f"http://{settings.HOST}:{settings.PORT}"
    
    async def handle_incoming_call(
        self,
        caller_phone: str,
        to_number: str,
        call_sid: str
    ) -> VoiceResponse:
        """Generate TwiML for incoming call"""
        
        # TODO: Look up business by to_number
        # TODO: Check if returning customer
        # TODO: Create conversation record
        
        conv_id = str(uuid.uuid4())
        business_id = "test_business"  # TODO: lookup
        lang = "pt"
        
        # Build greeting
        greeting = "BoomReception, boa tarde. Em que posso ajudar?"
        
        # Build gather URL
        gather_url = f"{self.base_url}/twilio/voice/gather?" \
                    f"businessId={business_id}&convId={conv_id}&" \
                    f"callerPhone={caller_phone}&lang={lang}"
        
        # Create TwiML response
        response = VoiceResponse()
        
        # Play greeting using TTS
        tts_url = f"{self.base_url}/twilio/voice/tts?text={greeting}&lang={lang}"
        response.play(tts_url)
        
        # Gather customer speech
        gather = Gather(
            input='speech',
            action=gather_url,
            method='POST',
            language='en-US',
            speech_timeout='auto',
            timeout=10
        )
        response.append(gather)
        
        # Redirect if no input
        response.redirect(gather_url, method='POST')
        
        return response
    
    async def handle_gather(
        self,
        business_id: str,
        conv_id: str,
        caller_phone: str,
        speech_result: str,
        confidence: float,
        language: str
    ) -> VoiceResponse:
        """Process customer speech and generate response"""
        
        # TODO: Load conversation history
        # TODO: Send to Claude for processing
        # TODO: Check if booking requested
        # TODO: Update conversation record
        
        # For now, simple echo response
        if speech_result:
            assistant_response = f"Entendi: {speech_result}. Como posso continuar a ajudar?"
        else:
            assistant_response = "Desculpe, não ouvi nada. Pode repetir?"
        
        # Build TwiML
        response = VoiceResponse()
        
        # Play assistant response
        tts_url = f"{self.base_url}/twilio/voice/tts?text={assistant_response}&lang={language}"
        response.play(tts_url)
        
        # Gather next input
        gather_url = f"{self.base_url}/twilio/voice/gather?" \
                    f"business_id={business_id}&convId={conv_id}&" \
                    f"callerPhone={caller_phone}&lang={language}"
        
        gather = Gather(
            input='speech',
            action=gather_url,
            method='POST',
            speech_timeout='auto'
        )
        response.append(gather)
        
        return response
    
    async def handle_call_status(
        self,
        call_sid: str,
        call_status: str,
        call_duration: int,
        business_id: str,
        conv_id: str
    ):
        """Handle call status callback"""
        
        # TODO: Update conversation with final status and duration
        # TODO: Trigger trait extraction if completed
        
        pass
    
    async def generate_speech(self, text: str, lang: str) -> bytes:
        """Generate TTS audio"""
        
        audio_data = await self.tts_client.synthesize(text, lang)
        return audio_data
