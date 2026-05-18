"""Complaint Model"""

from sqlalchemy import Column, String, DateTime, ForeignKey, Index
from sqlalchemy.sql import func

from app.database import Base


class Complaint(Base):
    """Customer complaint logged during a call"""

    __tablename__ = "complaints"

    # Primary Key
    id = Column(String(128), primary_key=True)

    # Relations
    business_id = Column(String(128), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id = Column(String(128), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)

    # Customer
    customer_phone = Column(String(20), nullable=False, index=True)
    customer_name = Column(String(255))

    # Complaint Content
    text = Column(String(4096), nullable=False)
    category = Column(String(50))          # service_quality, wait_time, pricing, staff, other
    sentiment = Column(String(20))         # negative, very_negative
    language = Column(String(5), default="en")

    # Resolution
    status = Column(String(20), nullable=False, default="open", index=True)   # open, in_review, resolved

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_complaints_business_created", "business_id", "created_at"),
    )
