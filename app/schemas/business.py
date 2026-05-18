"""Business Schemas"""

from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime


class BusinessBase(BaseModel):
    name: str
    owner_phone: str
    owner_name: Optional[str] = None
    primary_language: str = "pt"
    country: Optional[str] = None


class BusinessCreate(BusinessBase):
    site_url: Optional[str] = None


class BusinessResponse(BusinessBase):
    id: str
    status: str
    services: List[Dict] = []
    created_at: datetime
    
    class Config:
        from_attributes = True
