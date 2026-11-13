from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint
)
from .db import Base

def uid():
    return uuid4().hex

def now():
    return datetime.now(timezone.utc)

def pk():
    return Column(String(32), primary_key=True, default=uid)

class Organization(Base):
    __tablename__ = "organizations"
    id = pk()
    name = Column(String(160), unique=True, nullable=False)

class User(Base):
    __tablename__ = "users"
    id = pk()
    org_id = Column(String(32), ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    email = Column(String(200), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(20), nullable=False, index=True)
    skills = Column(JSON, nullable=False, default=list)
    area = Column(String(80), default="")
    active = Column(Boolean, default=True, nullable=False)

class Site(Base):
    __tablename__ = "sites"
    id = pk()
    org_id = Column(String(32), ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    city = Column(String(80), nullable=False)
    area = Column(String(80), nullable=False)
    timezone = Column(String(80), nullable=False, default="Europe/Moscow")

class Asset(Base):
    __tablename__ = "assets"
    id = pk()
    site_id = Column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    kind = Column(String(40), nullable=False)

class SLAPolicy(Base):
    __tablename__ = "sla_policies"
    id = pk()
    name = Column(String(120), nullable=False)
    priority = Column(String(2), nullable=False)
    reaction_minutes = Column(Integer, nullable=False)
    resolution_minutes = Column(Integer, nullable=False)
    allowed_pauses = Column(JSON, nullable=False, default=list)

class Contract(Base):
    __tablename__ = "contracts"
    id = pk()
    site_id = Column(String(32), ForeignKey("sites.id"), nullable=False, unique=True)
    policy_ids = Column(JSON, nullable=False)
    calendar = Column(JSON, nullable=False)

class Shift(Base):
    __tablename__ = "shifts"
    id = pk()
    engineer_id = Column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)

class Ticket(Base):
    __tablename__ = "tickets"
    id = pk()
    org_id = Column(String(32), ForeignKey("organizations.id"), nullable=False, index=True)
    site_id = Column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    asset_id = Column(String(32), ForeignKey("assets.id"), nullable=False)
    creator_id = Column(String(32), ForeignKey("users.id"), nullable=False)
    assignee_id = Column(String(32), ForeignKey("users.id"), nullable=True, index=True)
    priority = Column(String(2), nullable=False)
    kind = Column(String(40), nullable=False)
    description = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="new", index=True)
    version = Column(Integer, nullable=False, default=1)
    policy_snapshot = Column(JSON, nullable=False)
    reaction_deadline = Column(DateTime(timezone=True), nullable=False)
    resolution_deadline = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    duplicate_of = Column(String(32), nullable=True)
    __table_args__ = (Index("ix_ticket_site_kind_time", "site_id", "kind", "created_at"),)

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String(32), ForeignKey("tickets.id"), nullable=False, index=True)
    actor_id = Column(String(32), ForeignKey("users.id"), nullable=True)
    event_key = Column(String(150), unique=True, nullable=False)
    type = Column(String(50), nullable=False)
    at = Column(DateTime(timezone=True), nullable=False, default=now)
    data = Column(JSON, nullable=False, default=dict)

class Pause(Base):
    __tablename__ = "pauses"
    id = pk()
    ticket_id = Column(String(32), ForeignKey("tickets.id"), nullable=False, index=True)
    reason = Column(String(40), nullable=False)
    actor_id = Column(String(32), ForeignKey("users.id"), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)

class Worklog(Base):
    __tablename__ = "worklogs"
    id = pk()
    ticket_id = Column(String(32), ForeignKey("tickets.id"), nullable=False, index=True)
    engineer_id = Column(String(32), ForeignKey("users.id"), nullable=False)
    action_uuid = Column(String(36), nullable=False)
    summary = Column(String(2000), nullable=False, default="")
    checklist = Column(JSON, nullable=False, default=dict)
    materials = Column(JSON, nullable=False, default=list)
    client_at = Column(DateTime(timezone=True), nullable=False)
    server_at = Column(DateTime(timezone=True), nullable=False, default=now)
    __table_args__ = (UniqueConstraint("engineer_id", "action_uuid", name="uq_worklog_action"),)

class Attachment(Base):
    __tablename__ = "attachments"
    id = pk()
    ticket_id = Column(String(32), ForeignKey("tickets.id"), nullable=False, index=True)
    worklog_id = Column(String(32), ForeignKey("worklogs.id"), nullable=True)
    uploader_id = Column(String(32), ForeignKey("users.id"), nullable=False)
    storage_key = Column(String(180), unique=True, nullable=False)
    thumb_key = Column(String(180), nullable=False)
    checksum = Column(String(64), nullable=False)
    mime = Column(String(40), nullable=False)
    size = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

class Act(Base):
    __tablename__ = "acts"
    id = pk()
    ticket_id = Column(String(32), ForeignKey("tickets.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by = Column(String(32), ForeignKey("users.id"), nullable=True)
    reason = Column(String(1000), nullable=True)
    __table_args__ = (UniqueConstraint("ticket_id", "version", name="uq_act_version"),)

class Idempotency(Base):
    __tablename__ = "idempotency"
    id = pk()
    actor_id = Column(String(32), ForeignKey("users.id"), nullable=False)
    route = Column(String(100), nullable=False)
    client_uuid = Column(String(80), nullable=False)
    request_hash = Column(String(64), nullable=False)
    response = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("actor_id", "route", "client_uuid", name="uq_idempotency"),)

class Outbox(Base):
    __tablename__ = "outbox"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(160), nullable=False, unique=True)
    recipient = Column(String(200), nullable=False)
    subject = Column(String(200), nullable=False)
    body = Column(String(2000), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    sent_at = Column(DateTime(timezone=True), nullable=True)

class BoardEvent(Base):
    __tablename__ = "board_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(String(32), ForeignKey("organizations.id"), nullable=True, index=True)
    ticket_id = Column(String(32), ForeignKey("tickets.id"), nullable=False)
    kind = Column(String(40), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

class AuthAttempt(Base):
    __tablename__ = "auth_attempts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    fingerprint = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
