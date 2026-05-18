"""Customer Schemas"""

from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime


class CustomerResponse(BaseModel):
    phone: str
    business_id: str
    name: Optional[str] = None
    language: str
    total_visits: int = 0
    last_seen: Optional[datetime] = None
    traits: Optional[Dict] = None
    flags: List[str] = []
    score: Optional[Dict] = None
    total_spend: float = 0
    
    class Config:
        from_attributes = True
