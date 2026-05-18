"""User Model"""

from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func

from app.database import Base


class User(Base):
    """User authentication model"""
    
    __tablename__ = "users"
    
    # Primary Key (Firebase Auth UID)
    uid = Column(String(128), primary_key=True)
    
    # Identity
    email = Column(String(255), unique=True, nullable=False, index=True)
    phone = Column(String(20))
    name = Column(String(255), nullable=False)
    
    # Preferences
    ui_language = Column(String(5), default='en')
    
    # Business Association
    business_id = Column(String(128), index=True)
    
    # Role
    role = Column(String(20), nullable=False, default='owner')  # owner, staff, admin
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login = Column(DateTime(timezone=True))
    
    def __repr__(self):
        return f"<User(uid={self.uid}, email={self.email})>"
