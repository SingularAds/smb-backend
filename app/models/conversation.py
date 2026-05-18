"""Conversation Model"""

from sqlalchemy import Column, String, Integer, DateTime, Boolean, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Conversation(Base):
    """Call and message conversation model"""
    
    __tablename__ = "conversations"
    
    # Primary Key
    id = Column(String(128), primary_key=True)
    
    # Foreign Keys
    business_id = Column(String(128), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    
    # Identifiers
    conv_id = Column(String(128))
    call_sid = Column(String(128), index=True)
    
    # Participants
    customer_phone = Column(String(20), nullable=False, index=True)
    
    # Timing
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at = Column(DateTime(timezone=True))
    duration = Column(Integer)  # Seconds
    
    # Status & Outcome
    status = Column(String(20), nullable=False, index=True)  # active, completed, missed, etc.
    outcome = Column(String(20))  # booked, missed, transferred, etc.
    
    # Channel
    channel = Column(String(20))  # voice, whatsapp
    language = Column(String(5), nullable=False)
    
    # Customer Context
    is_new_customer = Column(Boolean, default=False)
    
    # Transcript (stored as JSONB array)
    transcript = Column(JSON, default=list)
    
    # AI Analysis
    summary = Column(String(2048))
    ai_notes = Column(JSON, default=list)
    sentiment = Column(String(20))  # positive, neutral, negative
    
    # Booking Result
    booking_id = Column(String(128), ForeignKey("bookings.id"))
    booking_confirmed = Column(Boolean, default=False)
    
    # Owner Status
    read = Column(Boolean, default=False)
    handled = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    business = relationship("Business", back_populates="conversations")
    
    # Indexes
    __table_args__ = (
        Index('idx_business_started', 'business_id', 'started_at'),
        Index('idx_customer_started', 'customer_phone', 'started_at'),
    )
    
    def __repr__(self):
        return f"<Conversation(id={self.id}, customer={self.customer_phone}, status={self.status})>"
