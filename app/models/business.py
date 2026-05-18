"""Business Model"""

from sqlalchemy import Column, String, JSON, DateTime, Boolean, Integer
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime

from app.database import Base


class Business(Base):
    """Business account model"""
    
    __tablename__ = "businesses"
    
    # Primary Key
    id = Column(String(128), primary_key=True)
    
    # Identity
    name = Column(String(255), nullable=False)
    owner_phone = Column(String(20), nullable=False, index=True)
    owner_name = Column(String(255))
    owner_uid = Column(String(128), index=True)  # Firebase Auth UID
    
    # Location & Language
    domain = Column(String(255))
    site_url = Column(String(512))
    country = Column(String(2))  # ISO 3166-1 alpha-2
    primary_language = Column(String(5), nullable=False, default='pt')
    location = Column(String(512))
    description = Column(String(2048))
    
    # Services (stored as JSONB)
    services = Column(JSON, default=list)
    
    # Business Hours
    hours = Column(JSON)
    closed_dates = Column(JSON, default=list)
    capacity = Column(Integer, default=30)
    total_seats = Column(Integer)
    
    # Phone Numbers
    phone_number = Column(String(20))
    twilio_number = Column(String(20), index=True)
    whatsapp_number = Column(String(20), index=True)
    owner_whatsapp = Column(String(20))
    local_number = Column(String(20))
    use_call_forwarding = Column(Boolean, default=False)
    
    # VAPI Integration
    vapi_assistant_id = Column(String(128))
    vapi_phone_number = Column(String(20))
    vapi_phone_number_id = Column(String(128))
    
    # WhatsApp
    wa_session_id = Column(String(512))
    wa_number = Column(String(20))
    auto_reply = Column(JSON)
    
    # Calendar Integration (stored as JSONB)
    calendar_config = Column(JSON)
    
    # Booking Platform
    booking_platform = Column(String(50))
    booking_config = Column(JSON)
    
    # Automation Settings (stored as JSONB)
    automations = Column(JSON, default=dict)
    notifications = Column(JSON, default=dict)
    features = Column(JSON, default=dict)
    
    # Scraping Data
    scrape_data = Column(JSON)
    
    # Webhook
    webhook_url = Column(String(512))
    
    # Status
    status = Column(String(20), nullable=False, default='active', index=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    trial_start_date = Column(DateTime(timezone=True))
    
    # Relationships
    bookings = relationship("Booking", back_populates="business", cascade="all, delete-orphan")
    customers = relationship("Customer", back_populates="business", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="business", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Business(id={self.id}, name={self.name})>"
