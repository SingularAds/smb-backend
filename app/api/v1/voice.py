"""Voice API Router (Twilio Integration)"""

from fastapi import APIRouter, Request, Response, Form
from typing import Optional
from twilio.twiml.voice_response import VoiceResponse, Gather
import uuid

from app.services.voice_service import VoiceService

router = APIRouter()
voice_service = VoiceService()


@router.post("/voice/webhook")
async def incoming_call(
    request: Request,
    From: str = Form(...),
    To: str = Form(...),
    CallSid: str = Form(...),
):
    """Handle incoming Twilio call"""
    
    # TODO: Validate Twilio signature
    
    # Generate TwiML response
    twiml = await voice_service.handle_incoming_call(
        caller_phone=From,
        to_number=To,
        call_sid=CallSid
    )
    
    return Response(content=str(twiml), media_type="application/xml")


@router.post("/voice/gather")
async def gather_speech(
    request: Request,
    SpeechResult: Optional[str] = Form(None),
    Confidence: Optional[float] = Form(None),
    businessId: str = Form(...),
    convId: str = Form(...),
    callerPhone: str = Form(...),
    lang: str = Form('en'),
):
    """Handle speech gathering loop"""
    
    # Process customer speech
    twiml = await voice_service.handle_gather(
        business_id=businessId,
        conv_id=convId,
        caller_phone=callerPhone,
        speech_result=SpeechResult or "",
        confidence=Confidence or 0.0,
        language=lang
    )
    
    return Response(content=str(twiml), media_type="application/xml")


@router.post("/voice/status")
async def call_status(
    request: Request,
    CallSid: str = Form(...),
    CallStatus: str = Form(...),
    CallDuration: Optional[int] = Form(None),
    businessId: str = Form(...),
    convId: str = Form(...),
):
    """Handle call status callback"""
    
    await voice_service.handle_call_status(
        call_sid=CallSid,
        call_status=CallStatus,
        call_duration=CallDuration or 0,
        business_id=businessId,
        conv_id=convId
    )
    
    return {"status": "ok"}


@router.get("/voice/tts")
async def text_to_speech(
    text: str,
    lang: str = 'en',
):
    """Generate TTS audio using ElevenLabs"""
    
    audio_data = await voice_service.generate_speech(text, lang)
    
    return Response(
        content=audio_data,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=3600"}
    )
