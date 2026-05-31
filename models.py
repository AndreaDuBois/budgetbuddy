from datetime import datetime
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey,
    Integer, String, Text
)
from sqlalchemy.orm import relationship
from database import Base


class KidProfile(Base):
    __tablename__ = "kid_profiles"

    id               = Column(Integer, primary_key=True, index=True)
    name             = Column(String, nullable=False)
    avatar_color     = Column(String, default="#6366f1")
    can_adjust_budgets = Column(Boolean, default=False)
    created_at       = Column(DateTime, default=datetime.utcnow)

    user             = relationship("User", back_populates="kid_profile", uselist=False)
    budget_entries   = relationship("BudgetEntry", back_populates="kid")
    receipts         = relationship("Receipt", back_populates="kid")


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    display_name    = Column(String, nullable=False)
    email           = Column(String, unique=True, index=True, nullable=False)
    password_hash   = Column(String, nullable=False)
    role            = Column(String, nullable=False)   # "parent" | "kid"
    created_at      = Column(DateTime, default=datetime.utcnow)

    kid_profile_id  = Column(Integer, ForeignKey("kid_profiles.id"), nullable=True)
    kid_profile     = relationship("KidProfile", back_populates="user", foreign_keys=[kid_profile_id])
    receipts        = relationship("Receipt", back_populates="uploaded_by_user")


class ShoppingEvent(Base):
    __tablename__ = "shopping_events"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    categories    = relationship("EventCategory", back_populates="event",
                                 cascade="all, delete-orphan", order_by="EventCategory.sort_order")
    budget_entries = relationship("BudgetEntry", back_populates="event")
    receipts      = relationship("Receipt", back_populates="event")


class EventCategory(Base):
    __tablename__ = "event_categories"

    id          = Column(Integer, primary_key=True, index=True)
    event_id    = Column(Integer, ForeignKey("shopping_events.id"), nullable=False)
    name        = Column(String, nullable=False)
    qty_needed  = Column(String, nullable=True)   # e.g. "3-5" stored as string
    notes       = Column(Text, nullable=True)
    sort_order  = Column(Integer, default=0)

    event          = relationship("ShoppingEvent", back_populates="categories")
    budget_entries = relationship("BudgetEntry", back_populates="category")
    receipt_items  = relationship("ReceiptLineItem", back_populates="category")


class BudgetEntry(Base):
    __tablename__ = "budget_entries"

    id              = Column(Integer, primary_key=True, index=True)
    kid_id          = Column(Integer, ForeignKey("kid_profiles.id"), nullable=False)
    event_id        = Column(Integer, ForeignKey("shopping_events.id"), nullable=False)
    category_id     = Column(Integer, ForeignKey("event_categories.id"), nullable=False)
    budgeted_amount = Column(Float, nullable=False, default=0.0)
    spent_amount    = Column(Float, nullable=False, default=0.0)  # denormalized; recalculated on confirm
    last_updated_by = Column(String, nullable=True)               # "parent" | "kid"
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    kid      = relationship("KidProfile", back_populates="budget_entries")
    event    = relationship("ShoppingEvent", back_populates="budget_entries")
    category = relationship("EventCategory", back_populates="budget_entries")


class Receipt(Base):
    __tablename__ = "receipts"

    id                  = Column(Integer, primary_key=True, index=True)
    kid_id              = Column(Integer, ForeignKey("kid_profiles.id"), nullable=False)
    event_id            = Column(Integer, ForeignKey("shopping_events.id"), nullable=False)
    uploaded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    upload_method       = Column(String, nullable=False)  # "image"|"pdf"|"paste"|"manual"
    store_name          = Column(String, nullable=True)
    receipt_date        = Column(Date, nullable=True)
    raw_text            = Column(Text, nullable=True)
    file_path           = Column(String, nullable=True)   # relative path under UPLOAD_DIR
    status              = Column(String, default="pending_review")  # "pending_review"|"confirmed"
    total_amount        = Column(Float, nullable=True)
    groq_raw_response   = Column(Text, nullable=True)
    notes               = Column(Text, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)

    kid                = relationship("KidProfile", back_populates="receipts")
    event              = relationship("ShoppingEvent", back_populates="receipts")
    uploaded_by_user   = relationship("User", back_populates="receipts")
    line_items         = relationship("ReceiptLineItem", back_populates="receipt",
                                      cascade="all, delete-orphan")


class ReceiptLineItem(Base):
    __tablename__ = "receipt_line_items"

    id            = Column(Integer, primary_key=True, index=True)
    receipt_id    = Column(Integer, ForeignKey("receipts.id"), nullable=False)
    description   = Column(String, nullable=False)
    quantity      = Column(Integer, default=1)
    unit_price    = Column(Float, nullable=True)
    total_price   = Column(Float, nullable=False)
    category_id   = Column(Integer, ForeignKey("event_categories.id"), nullable=True)
    confidence    = Column(Float, nullable=True)   # 0.0–1.0 from Groq; None for manual
    review_status = Column(String, default="pending")  # "pending"|"confirmed"|"skipped"
    is_in_budget  = Column(Boolean, default=True)

    receipt  = relationship("Receipt", back_populates="line_items")
    category = relationship("EventCategory", back_populates="receipt_items")
