"""Conversation Schemas"""

from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime


class ConversationResponse(BaseModel):
    id: str
    business_id: str
    customer_phone: str
    status: str
    outcome: Optional[str] = None
    channel: Optional[str] = None
    language: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration: Optional[int] = None
    transcript: List[Dict] = []
    summary: Optional[str] = None
    sentiment: Optional[str] = None
    read: bool = False
    
    class Config:
        from_attributes = True
