"""VAPI Integration Service - Handles VAPI API calls for assistant and phone number management"""

import requests
import logging
from typing import Optional, Dict, Any
from app.config import settings

logger = logging.getLogger(__name__)

VAPI_BASE_URL = "https://api.vapi.ai"
VAPI_API_KEY = settings.VAPI_API_KEY
DEFAULT_ASSISTANT_ID = settings.VAPI_DEFAULT_ASSISTANT_ID


class VAPIService:
    """Service for managing VAPI assistants and phone numbers"""
    
    def __init__(self, api_key: str = VAPI_API_KEY):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    
    def get_assistant(self, assistant_id: str) -> Dict[str, Any]:
        """
        Fetch an assistant by ID
        
        Args:
            assistant_id: VAPI assistant ID
            
        Returns:
            Assistant data dict
        """
        try:
            url = f"{VAPI_BASE_URL}/assistant/{assistant_id}"
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            logger.info(f"✓ Fetched assistant {assistant_id}")
            return response.json()
        except Exception as e:
            logger.error(f"✗ Failed to fetch assistant {assistant_id}: {str(e)}")
            raise

    def replace_placeholders(self, text: str, business_name: str, business_id: str) -> str:
        if not text:
            return text

        replacements = {
            "{{businessName}}": business_name,
            "{{business_name}}": business_name,
            "{{businessId}}": business_id,
            "{{business_id}}": business_id,
        }

        for key, value in replacements.items():
            text = text.replace(key, value)

        return text    
    
    def create_assistant(
        self,
        name: str,
        business_name: str,
        business_id: str,
        system_prompt: str,
        first_message: Optional[str] = None,
        end_call_message: Optional[str] = None,
        voicemail_message: Optional[str] = None,
        template_assistant_id: str = DEFAULT_ASSISTANT_ID,
    ) -> Dict[str, Any]:
        """
        Create a new assistant for a business (cloned from template)
        
        Args:
            name: Assistant name
            business_name: Business name for personalization
            system_prompt: System prompt for the assistant
            first_message: Custom first message
            end_call_message: Custom end call message
            voicemail_message: Custom voicemail message
            template_assistant_id: Base assistant to clone from
            
        Returns:
            New assistant data dict with id
        """
        try:
            # Fetch template assistant to get structure
            template = self.get_assistant(template_assistant_id)
            print(template)
            # Build new assistant payload
            payload = {
                "name": name,
                "firstMessage":first_message or f"Hi, thanks for calling {business_name}! How can I assist you today?",
                "voice": template.get("voice", {"voiceId": "Elliot", "provider": "vapi"}),
                "model": {
                    "provider": "openai",
                    "model": "gpt-4.1",
                    "temperature": 0.5,
                    "toolIds": template.get("model", {}).get("toolIds", []),
                    "messages": [
                        {
                            "role": "system",
                            "content": self.replace_placeholders(
                                template.get("model", {}).get("messages", [
                                    {
                                        "role": "system",
                                        "content": system_prompt,
                                    }
                                ])[0].get("content", system_prompt),
                                business_name,
                                business_id,
                            ),
                        }
                    ],
                },
                "transcriber": template.get("transcriber", {
                    "provider": "deepgram",
                    "model": "nova-3",
                    "language": "multi",
                    "endpointing": 150,
                    "fallbackPlan": {
                        "transcribers": [
                            {
                                "model": "flux-general-en",
                                "language": "en",
                                "provider": "deepgram",
                            }
                        ]
                    },
                }),
                "startSpeakingPlan": template.get("startSpeakingPlan", {
                    "waitSeconds": 0.4,
                    "smartEndpointingEnabled": "livekit",
                }),
                "endCallPhrases": template.get("endCallPhrases", ["goodbye", "talk to you soon"]),
                "clientMessages": template.get("clientMessages", [
                    "conversation-update",
                    "function-call",
                    "hang",
                    "model-output",
                    "speech-update",
                    "status-update",
                    "transfer-update",
                    "transcript",
                    "tool-calls",
                    "user-interrupted",
                    "voice-input",
                    "workflow.node.started",
                    "assistant.started",
                ]),
                "serverMessages": template.get("serverMessages", [
                    "conversation-update",
                    "end-of-call-report",
                    "function-call",
                    "hang",
                    "speech-update",
                    "status-update",
                    "tool-calls",
                    "transfer-destination-request",
                    "handoff-destination-request",
                    "user-interrupted",
                    "assistant.started",
                ]),
                "hipaaEnabled": False,
                "DenoisingEnabled": False,
            }
            
            url = f"{VAPI_BASE_URL}/assistant"
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            
            new_assistant = response.json()
            logger.info(f"✓ Created new assistant: {new_assistant.get('id')} for {business_name}")
            return new_assistant
            
        except Exception as e:
            logger.error(f"✗ Failed to create assistant for {business_name}: {str(e)}")
            raise
    
    def create_phone_number(
        self,
        assistant_id: str,
        area_code: str = "951",
    ) -> Dict[str, Any]:
        """
        Create a new phone number for an assistant
        
        Args:
            assistant_id: VAPI assistant ID to link
            area_code: Desired area code (e.g., "951")
            
        Returns:
            Phone number data dict with id and number
        """
        try:
            payload = {
                "provider": "vapi",
                "assistantId": assistant_id,
                "numberDesiredAreaCode": area_code,
            }
            
            url = f"{VAPI_BASE_URL}/phone-number"
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            
            phone_data = response.json()
            logger.info(f"✓ Created phone number {phone_data.get('number')} for assistant {assistant_id}")
            return phone_data
            
        except Exception as e:
            logger.error(f"✗ Failed to create phone number for assistant {assistant_id}: {str(e)}")
            raise
    
    def update_phone_number_assistant(
        self,
        phone_number_id: str,
        assistant_id: str,
    ) -> Dict[str, Any]:
        """
        Update phone number to use a different assistant
        
        Args:
            phone_number_id: VAPI phone number ID
            assistant_id: New assistant ID to assign
            
        Returns:
            Updated phone number data
        """
        try:
            payload = {
                "assistantId": assistant_id,
            }
            
            url = f"{VAPI_BASE_URL}/phone-number/{phone_number_id}"
            response = requests.patch(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            
            phone_data = response.json()
            logger.info(f"✓ Updated phone number {phone_number_id} with assistant {assistant_id}")
            return phone_data
            
        except Exception as e:
            logger.error(f"✗ Failed to update phone number {phone_number_id}: {str(e)}")
            raise


# Global instance
vapi_service = VAPIService()
