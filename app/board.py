"""SQL-backed role-scoped dashboard; never derives KPIs from a 500-row slice."""
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from .api import fmt, serialize_ticket
from .db import get_db
from .models import Pause, Ticket, User, now
from .security import current_user
from .ticket_queries import OPEN, visibility

router = APIRouter(prefix="/api/v1/board", tags=["board"])


@router.get("/summary")
def summary(user: User = Depends(current_user), db: Session = Depends(get_db)):
    allowed = visibility(user)
    grouped = db.execute(select(Ticket.status, func.count(Ticket.id))
                         .where(allowed).group_by(Ticket.status)).all()
    by_status = {key: count for key, count in grouped}
    total = sum(by_status.values())
    open_count = sum(by_status.get(key, 0) for key in OPEN)
    active = (allowed, Ticket.status.in_(OPEN))
    unassigned = db.scalar(select(func.count(Ticket.id)).where(
        *active, Ticket.assignee_id.is_(None))) or 0
    p1 = db.scalar(select(func.count(Ticket.id)).where(
        *active, Ticket.priority == "P1")) or 0
    current = now()
    pause_active = exists(select(Pause.id).where(
        Pause.ticket_id == Ticket.id, Pause.ended_at.is_(None)))
    due_scope = (*active, ~pause_active)
    overdue = db.scalar(select(func.count(Ticket.id)).where(
        *due_scope, Ticket.resolution_deadline < current)) or 0
    due_soon = db.scalar(select(func.count(Ticket.id)).where(
        *due_scope, Ticket.resolution_deadline >= current,
        Ticket.resolution_deadline <= current + timedelta(hours=1))) or 0
    upcoming = db.scalars(select(Ticket).where(*due_scope)
                          .order_by(Ticket.resolution_deadline.asc(), Ticket.id.desc())
                          .limit(6)).all()
    recent = db.scalars(select(Ticket).where(allowed)
                        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
                        .limit(7)).all()
    return {
        "total": total, "open": open_count, "closed": by_status.get("closed", 0),
        "unassigned": unassigned, "open_p1": p1, "overdue": overdue,
        "due_soon": due_soon,
        "by_status": by_status, "upcoming": [serialize_ticket(db, t) for t in upcoming],
        "recent": [serialize_ticket(db, t) for t in recent],
        "observed_at": fmt(current),
    }
