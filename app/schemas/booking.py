"""Booking Schemas"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class BookingBase(BaseModel):
    customer_phone: str
    customer_name: str
    service_name: str
    service_duration: int  # minutes
    service_price: float
    datetime: datetime
    source: str =Field(default="direct")


class BookingCreate(BookingBase):
    pass


class BookingUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


class BookingResponse(BookingBase):
    id: str
    business_id: str
    status: str
    created_at: datetime
    confirmed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class AvailableSlotsRequest(BaseModel):
    business_id: str
    service_name: str
    date: str  # YYYY-MM-DD
