"""Bound, expiring keyset cursors for authenticated registry scans.

Cursors contain no personal details, are signed using an independent salt, and
cannot be reused for a different user, search, sort, active flag or page size.
OFFSET pagination remains supported by the existing /tickets/search endpoint.
"""
import hashlib
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from .api import serialize_ticket
from .db import get_db
from .models import Ticket, User
from .security import SESSION_SECRET, current_user
from .sla import aware
from .ticket_queries import filtered_ticket_query, ticket_order

router = APIRouter(prefix="/api/v1/tickets", tags=["registry"])
STATUS = Literal["new", "triage", "assigned", "accepted", "in_progress",
                 "approval", "returned", "closed", "cancelled"]
PRIORITY = Literal["P1", "P2", "P3", "P4"]
SORT = Literal["newest", "deadline", "priority"]
MAX_CURSOR_AGE = 3600
SIGNER = URLSafeTimedSerializer(SESSION_SECRET, salt="fieldpulse-ticket-cursor-v1")


def query_signature(*, q, status, priority, sort, active, page_size):
    filters = {"q": q.strip(), "status": status, "priority": priority, "sort": sort,
               "active": active, "page_size": page_size}
    return hashlib.sha256(json.dumps(filters, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def key_for(row, sort):
    if sort == "priority":
        return [row.priority, aware(row.resolution_deadline).isoformat(), row.id]
    if sort == "deadline":
        return [aware(row.resolution_deadline).isoformat(), row.id]
    return [aware(row.created_at).isoformat(), row.id]


def seek_condition(key, sort):
    # Timestamp is from a validated HMAC-signed payload; fail closed on
    # unexpected types and dimensions even if a different signer is reused.
    expected = 3 if sort == "priority" else 2
    if not isinstance(key, list) or len(key) != expected:
        raise ValueError("Invalid cursor key")
    stamp, identifier = key[-2:]
    if not isinstance(stamp, str) or not isinstance(identifier, str) or len(identifier) != 32:
        raise ValueError("Invalid cursor fields")
    moment = datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        raise ValueError("Missing timezone")
    if sort == "newest":
        return or_(Ticket.created_at < moment,
                   and_(Ticket.created_at == moment, Ticket.id < identifier))
    if sort == "deadline":
        return or_(Ticket.resolution_deadline > moment,
                   and_(Ticket.resolution_deadline == moment, Ticket.id < identifier))
    priority = key[0]
    if priority not in ("P1", "P2", "P3", "P4"):
        raise ValueError("Invalid priority")
    return or_(
        Ticket.priority > priority,
        and_(Ticket.priority == priority, Ticket.resolution_deadline > moment),
        and_(Ticket.priority == priority, Ticket.resolution_deadline == moment,
             Ticket.id < identifier),
    )


@router.get("/seek")
def seek_tickets(
    page_size: int = Query(20, ge=10, le=100),
    q: str = Query("", max_length=100),
    status: STATUS | None = None,
    priority: PRIORITY | None = None,
    sort: SORT = "newest",
    active: bool = False,
    cursor: str | None = Query(None, max_length=1500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    signature = query_signature(q=q, status=status, priority=priority,
                                sort=sort, active=active, page_size=page_size)
    query = filtered_ticket_query(user, q=q, status=status, priority=priority,
                                  active=active)
    if cursor:
        try:
            decoded = SIGNER.loads(cursor, max_age=MAX_CURSOR_AGE)
            if (not isinstance(decoded, dict) or decoded.get("v") != 1 or
                    decoded.get("u") != user.id or decoded.get("h") != signature):
                raise ValueError("Cursor context mismatch")
            query = query.where(seek_condition(decoded.get("k"), sort))
        except (BadSignature, SignatureExpired, ValueError, TypeError, OverflowError):
            raise HTTPException(422, detail="Курсор недействителен или не соответствует фильтрам")
    rows = db.scalars(query.order_by(*ticket_order(sort)).limit(page_size + 1)).all()
    more = len(rows) > page_size
    shown = rows[:page_size]
    next_token = None
    if more and shown:
        next_token = SIGNER.dumps({
            "v": 1, "u": user.id, "h": signature,
            "k": key_for(shown[-1], sort),
        })
    return {
        "items": [serialize_ticket(db, row) for row in shown],
        "next_cursor": next_token,
        "has_more": more, "page_size": page_size, "sort": sort,
        "mode": "keyset",
    }
