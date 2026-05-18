"""Businesses API Router - Business management and onboarding"""

import hmac

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

from app import firestore as fs
from app.config import settings
from app.services.vapi_integration import vapi_service
from app.services.prompt_service import prompt_service
from app.services.ai_service import AIService
import logging


def _verify_auth(request: Request) -> None:
    expected = settings.VAPI_AUTHENTICATION_SECRET_KEY.strip()
    if not expected:
        return  # not configured — skip in dev
    header_name = settings.VAPI_AUTHENTICATION_HEADER_NAME.strip()
    received = request.headers.get(header_name, "")
    if not received or not hmac.compare_digest(received.strip(), expected):
        raise HTTPException(status_code=403, detail="Forbidden: invalid credentials")

logger = logging.getLogger(__name__)

router = APIRouter()


class BusinessOnboardingRequest(BaseModel):
    """Request to onboard/create a new business with VAPI integration"""
    businessId: str = Field(..., example="my-business-123", description="Unique business ID")
    businessName: str = Field(..., example="Wellness Partners", description="Business name")
    systemPrompt: Optional[str] = Field(None, description="Custom system prompt (uses template assistant's prompt if not provided)")
    firstMessage: Optional[str] = Field(None, description="Custom first message")
    endCallMessage: Optional[str] = Field(None, description="Custom end call message")
    voicemailMessage: Optional[str] = Field(None, description="Custom voicemail message")
    areaCode: Optional[str] = Field("951", description="Phone number area code")
    templateAssistantId: Optional[str] = Field(None, description="Template assistant to clone from (uses default if not provided)")


class GenerateBusinessPromptRequest(BaseModel):
    """Request for generating and storing business prompt."""
    scrapeWebsite: bool = Field(True, description="If true, scrape business website before generating prompt")


@router.post("/onboarding", status_code=201)
async def onboard_business(body: BusinessOnboardingRequest):
    """
    Onboard a new business:
    1. Fetch template assistant to get system prompt and substitute businessId / businessName
    2. Create a new VAPI assistant (cloned from template)
    3. Create a new phone number
    4. Assign assistant to phone number
    5. Store details in Firestore

    Returns: All assistant, phone, and business details
    """
    try:
        logger.info(f"Starting onboarding for business: {body.businessName} ({body.businessId})")

        template_assistant_id = body.templateAssistantId or "6ea20463-f52e-4ad5-8454-97ace9b2ed78"

        if body.systemPrompt:
            system_prompt = body.systemPrompt
        else:
            logger.info("Fetching system prompt from template assistant...")
            template_assistant = vapi_service.get_assistant(template_assistant_id)
            raw_prompt = (
                template_assistant
                .get("model", {})
                .get("messages", [{}])[0]
                .get("content", "")
            )
            if not raw_prompt:
                raise Exception("Template assistant has no system prompt in model.messages[0].content")
            system_prompt = (
                raw_prompt
                .replace("{{businessId}}", body.businessId)
                .replace("{{businessName}}", body.businessName)
            )
            logger.info("Using template assistant system prompt with business substitutions")

        logger.info("Step 1: Creating VAPI assistant...")
        new_assistant = vapi_service.create_assistant(
            name=f"{body.businessName} AI Receptionist",
            business_name=body.businessName,
            business_id=body.businessId,
            system_prompt=system_prompt,
            first_message=body.firstMessage,
            end_call_message=body.endCallMessage,
            voicemail_message=body.voicemailMessage,
            template_assistant_id=template_assistant_id,
        )
        assistant_id = new_assistant.get("id")
        logger.info(f"Assistant created: {assistant_id}")

        logger.info("Step 2: Creating VAPI phone number...")
        phone_data = vapi_service.create_phone_number(
            assistant_id=assistant_id,
            area_code=body.areaCode,
        )
        phone_id = phone_data.get("id")
        phone_number = phone_data.get("number")
        logger.info(f"Phone number created: {phone_number} ({phone_id})")

        logger.info("Step 3: Storing business details in Firestore...")
        business_data = {
            "id": body.businessId,
            "name": body.businessName,
            "vapiAssistantId": assistant_id,
            "vapiPhoneNumberId": phone_id,
            "vapiPhoneNumber": phone_number,
            "vapiAssistantData": new_assistant,
            "vapiPhoneData": phone_data,
            "createdAt": datetime.utcnow().isoformat(),
            "status": "active",
        }
        fs.set_business(body.businessId, business_data)
        logger.info("Business details stored in Firestore")

        response = {
            "success": True,
            "businessId": body.businessId,
            "businessName": body.businessName,
            "assistant": {
                "id": assistant_id,
                "name": new_assistant.get("name"),
                "createdAt": new_assistant.get("createdAt"),
                "model": new_assistant.get("model"),
                "voice": new_assistant.get("voice"),
                "firstMessage": new_assistant.get("firstMessage"),
                "endCallMessage": new_assistant.get("endCallMessage"),
                "voicemailMessage": new_assistant.get("voicemailMessage"),
            },
            "phoneNumber": {
                "id": phone_id,
                "number": phone_number,
                "status": phone_data.get("status"),
                "createdAt": phone_data.get("createdAt"),
                "assistantId": assistant_id,
            },
            "onboardedAt": datetime.utcnow().isoformat(),
            "message": f"Business '{body.businessName}' onboarded successfully with phone number {phone_number}",
        }

        logger.info(f"Onboarding completed for {body.businessName}")
        return response

    except Exception as e:
        error_msg = f"Onboarding failed for {body.businessName}: {str(e)}"
        logger.error(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)


@router.get("/{business_id}")
async def get_business(business_id: str):
    """Get business details including VAPI integration info"""
    try:
        business = fs.get_business_by_id(business_id)
        if not business:
            raise HTTPException(status_code=404, detail="Business not found")

        return {
            "id": business.get("id"),
            "name": business.get("name"),
            "vapiAssistantId": business.get("vapiAssistantId"),
            "vapiPhoneNumberId": business.get("vapiPhoneNumberId"),
            "vapiPhoneNumber": business.get("vapiPhoneNumber"),
            "createdAt": business.get("createdAt"),
            "status": business.get("status"),
        }
    except Exception as e:
        logger.error(f"Error fetching business {business_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{business_id}/generate-prompt")
async def generate_business_prompt(request: Request, business_id: str, body: GenerateBusinessPromptRequest | None = None):
    _verify_auth(request)
    """Generate and save a business-specific VAPI prompt.

    This endpoint should be called once onboarding is complete.
    """
    try:
        business = fs.get_business_by_id(business_id)
        if not business:
            raise HTTPException(status_code=404, detail="Business not found")

        scrape_website = True if body is None else body.scrapeWebsite

        scraped_data: dict | None = None
        site_url = business.get("siteUrl") or business.get("site_url") or business.get("scrapedUrl") or business.get("scraped_url")
        if scrape_website and site_url:
            logger.info("Generating prompt: scraping website %s for business %s", site_url, business_id)
            try:
                ai = AIService()
                scraped_data = await ai.scrape_website(site_url)
            except Exception as scrape_err:
                logger.warning("Website scrape failed for business %s: %s", business_id, scrape_err)

        generated_prompt = await prompt_service.generate(business, scraped_data)

        fs.merge_business_doc(
            business_id,
            {
                "vapiPrompt": generated_prompt,
                "vapiPromptUpdatedAt": datetime.utcnow().isoformat(),
            },
        )

        logger.info("Prompt generated and saved for business %s", business_id)
        return {
            "success": True,
            "businessId": business.get("id"),
            "businessName": business.get("name"),
            "prompt": generated_prompt,
            "saved": True,
            "message": "Prompt generated and saved successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error generating prompt for business %s: %s", business_id, str(e))
        raise HTTPException(status_code=500, detail=str(e))