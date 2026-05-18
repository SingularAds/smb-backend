"""Booking Model"""

from sqlalchemy import Column, String, Integer, Numeric, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Booking(Base):
    """Customer booking/appointment model"""
    
    __tablename__ = "bookings"
    
    # Primary Key
    id = Column(String(128), primary_key=True)
    
    # Foreign Keys
    business_id = Column(String(128), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    
    # Customer Information
    customer_phone = Column(String(20), nullable=False, index=True)
    customer_name = Column(String(255), nullable=False)
    
    # Service Details
    service_name = Column(String(255), nullable=False)
    service_duration = Column(Integer, nullable=False)  # Minutes
    service_price = Column(Numeric(10, 2), nullable=False)
    original_price = Column(Numeric(10, 2))
    
    # Scheduling
    datetime = Column(DateTime(timezone=True), nullable=False, index=True)
    
    # Discount (stored as JSON)
    discount = Column(String(1024))  # JSON string
    
    # Status
    status = Column(String(20), nullable=False, default='pending', index=True)
    source = Column(String(20), nullable=False)  # whatsapp, voice, platform, direct
    
    # Calendar Integration
    calendar_event_id = Column(String(255))
    
    # Restaurant-specific
    party_size = Column(Integer)
    
    # Notes
    notes = Column(String(2048))
    
    # External Platform
    platform_booking_id = Column(String(255))
    platform_confirmation = Column(String(255))
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    confirmed_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    cancelled_at = Column(DateTime(timezone=True))
    
    # Relationships
    business = relationship("Business", back_populates="bookings")
    
    # Indexes
    __table_args__ = (
        Index('idx_business_datetime', 'business_id', 'datetime'),
        Index('idx_business_status', 'business_id', 'status'),
        Index('idx_customer_created', 'customer_phone', 'created_at'),
    )
    
    def __repr__(self):
        return f"<Booking(id={self.id}, customer={self.customer_name}, service={self.service_name})>"
