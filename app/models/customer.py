"""Customer Model"""

from sqlalchemy import Column, String, Integer, Numeric, DateTime, Boolean, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Customer(Base):
    """Customer profile model"""
    
    __tablename__ = "customers"
    
    # Composite Primary Key (phone + business_id)
    phone = Column(String(20), primary_key=True)
    business_id = Column(String(128), ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    
    # Identity
    name = Column(String(255))
    customer_name = Column(String(255))
    
    # Preferences
    language = Column(String(5), nullable=False, default='pt')
    
    # Visit History
    total_visits = Column(Integer, default=0)
    visit_count = Column(Integer, default=0)
    last_seen = Column(DateTime(timezone=True), index=True)
    last_visit = Column(DateTime(timezone=True))
    total_messages = Column(Integer, default=0)
    
    # Opt-in/Opt-out
    wa_opt_in = Column(Boolean, default=False)
    reminder_opt_out = Column(Boolean, default=False)
    
    # Referral Program
    referral_code = Column(String(50))
    referred_by = Column(String(20))
    referral_discount = Column(Numeric(5, 2))
    referral_discount_used = Column(Boolean, default=False)
    pending_discount = Column(Numeric(5, 2))
    pending_discount_used = Column(Boolean, default=False)
    
    # AI-Extracted Traits (stored as JSONB)
    traits = Column(JSON)
    
    # Classification
    flags = Column(JSON, default=list)  # ["vip", "no_show_risk", "complaint"]
    ai_notes = Column(JSON, default=list)
    
    # RFM Score (stored as JSONB)
    score = Column(JSON)
    
    # Financial
    total_spend = Column(Numeric(10, 2), default=0)
    
    # Metadata
    is_new_customer = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_trait_extract_at = Column(DateTime(timezone=True))
    
    # Relationships
    business = relationship("Business", back_populates="customers")
    
    # Indexes
    __table_args__ = (
        Index('idx_business_visits', 'business_id', 'total_visits'),
        Index('idx_business_last_seen', 'business_id', 'last_seen'),
    )
    
    def __repr__(self):
        return f"<Customer(phone={self.phone}, name={self.name})>"
