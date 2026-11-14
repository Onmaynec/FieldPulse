"""SLA reconciliation and reproducible site-level operational reporting.

All metrics are derived from persisted Ticket snapshots, Pause rows and immutable
Event entries, never mutable current contract defaults.
"""
from collections import defaultdict
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Event, Outbox, Pause, Site, Ticket, User, now
from .security import roles
from .sla import aware, working_minutes
from .worker_health import snapshot
from .api import fmt, percentile

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
STAFF = roles("admin", "manager", "dispatcher")
BREACH_TYPES = ("sla_reaction_breached", "sla_resolution_breached")
PAUSE_TYPES = ("sla_pause_started", "sla_pause_ended")


def minutes(start, end, snapshot):
    if not start or not end:
        return None
    return round(working_minutes(start, end, snapshot["calendar"]), 2)


def load_violations(db, ids):
    if not ids:
        return {}
    records = db.scalars(select(Event).where(
        Event.ticket_id.in_(ids), Event.type.in_(BREACH_TYPES))).all()
    results = defaultdict(set)
    for event in records:
        results[event.ticket_id].add(event.type)
    return results


def evidence(ticket, events, pauses, at=None):
    at = at or now()
    calendar = ticket.policy_snapshot["calendar"]
    reaction_at = ticket.accepted_at or at
    resolution_at = ticket.finished_at or at
    reaction = minutes(ticket.created_at, reaction_at, ticket.policy_snapshot)
    resolution = minutes(ticket.created_at, resolution_at, ticket.policy_snapshot)
    paused = any(p.ended_at is None for p in pauses)
    pause_minutes = round(sum(
        working_minutes(p.started_at, p.ended_at or at, calendar)
        for p in pauses
    ), 2)
    # Open paused tickets cannot be labelled newly overdue until the pause ends.
    react_overdue = aware(reaction_at) > aware(ticket.reaction_deadline)
    resolve_overdue = (not paused and aware(resolution_at) > aware(ticket.resolution_deadline))
    return {
        "ticket_id": ticket.id, "site_id": ticket.site_id,
        "status": ticket.status, "priority": ticket.priority,
        "reaction_minutes": reaction,
        "resolution_minutes_raw": resolution,
        "approved_pause_minutes": pause_minutes,
        "paused": paused,
        "reaction_deadline": fmt(ticket.reaction_deadline),
        "resolution_deadline": fmt(ticket.resolution_deadline),
        "accepted_at": fmt(ticket.accepted_at),
        "finished_at": fmt(ticket.finished_at),
        "reaction_breached": bool("sla_reaction_breached" in events or
                                  (react_overdue and ticket.status not in ("cancelled",) and
                                   (ticket.accepted_at is not None or ticket.status not in ("closed",)))),
        "resolution_breached": bool("sla_resolution_breached" in events or
                                    (resolve_overdue and ticket.status not in ("cancelled",) and
                                     (ticket.finished_at is not None or ticket.status not in ("closed",)))),
        "recorded_violations": sorted(events),
        "snapshot_policy": ticket.policy_snapshot.get("policy_name"),
    }


@router.get("/sites")
def sites_report(user: User = Depends(STAFF), db: Session = Depends(get_db)):
    sites = db.scalars(select(Site).order_by(Site.name, Site.id)).all()
    tickets = db.scalars(select(Ticket).order_by(Ticket.id)).all()
    violations = load_violations(db, [t.id for t in tickets])
    groups = defaultdict(list)
    for t in tickets:
        groups[t.site_id].append(t)
    out = []
    for site in sites:
        items = groups[site.id]
        reactions = [
            minutes(t.created_at, t.accepted_at, t.policy_snapshot)
            for t in items if t.accepted_at is not None
        ]
        breaches = sum(bool(violations.get(t.id)) for t in items)
        out.append({
            "site_id": site.id,
            "site": site.name,
            "city": site.city,
            "area": site.area,
            "timezone": site.timezone,
            "total": len(items),
            "open": sum(t.status not in ("closed", "cancelled") for t in items),
            "closed": sum(t.status == "closed" for t in items),
            "reaction_p50_minutes": percentile(reactions, 0.50),
            "reaction_p95_minutes": percentile(reactions, 0.95),
            "breach_count": breaches,
            "breach_percent": round(100 * breaches / len(items), 2) if items else 0,
            "repeat_count": sum(bool(t.duplicate_of) for t in items),
        })
    return {"sites": out, "total_sites": len(out)}


@router.get("/tickets/{ticket_id}/reconcile")
def ticket_reconcile(ticket_id: str, user: User = Depends(STAFF),
                     db: Session = Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, detail="Заявка не найдена")
    events = db.scalars(select(Event).where(
        Event.ticket_id == ticket_id).order_by(Event.id)).all()
    pauses = db.scalars(select(Pause).where(
        Pause.ticket_id == ticket_id).order_by(Pause.started_at)).all()
    record = evidence(t, {e.type for e in events if e.type in BREACH_TYPES}, pauses)
    record["pauses"] = [{
        "reason": p.reason, "started_at": fmt(p.started_at),
        "ended_at": fmt(p.ended_at),
        "working_minutes": minutes(p.started_at, p.ended_at or now(), t.policy_snapshot),
    } for p in pauses]
    record["timeline"] = [{
        "type": e.type, "at": fmt(e.at), "actor_id": e.actor_id, "data": e.data,
    } for e in events]
    return record


@router.get("/operations")
def operations(user: User = Depends(STAFF), db: Session = Depends(get_db)):
    pending = db.scalar(select(func.count(Outbox.id)).where(Outbox.sent_at.is_(None))) or 0
    pauses = db.scalar(select(func.count(Pause.id)).where(Pause.ended_at.is_(None))) or 0
    unresolved = db.scalar(select(func.count(Ticket.id)).where(
        Ticket.status.notin_(("closed", "cancelled")))) or 0
    alerts = db.scalar(select(func.count(Event.id)).where(Event.type.in_(BREACH_TYPES))) or 0
    oldest = db.scalar(select(func.min(Outbox.created_at)).where(Outbox.sent_at.is_(None)))
    last_event = db.scalar(select(func.max(Event.at)))
    unassigned = db.scalar(select(func.count(Ticket.id)).where(
        Ticket.assignee_id.is_(None), Ticket.status.in_((
            "new", "triage", "assigned", "accepted", "in_progress", "approval", "returned")))) or 0
    critical = db.scalar(select(func.count(Ticket.id)).where(
        Ticket.priority == "P1", Ticket.status.notin_(("closed", "cancelled")))) or 0
    current = now()
    return {
        "pending_mock_notifications": pending,
        "active_sla_pauses": pauses,
        "open_tickets": unresolved,
        "recorded_breach_events": alerts,
        "unassigned_open_tickets": unassigned,
        "open_p1_tickets": critical,
        "oldest_pending_mock_at": oldest.isoformat() if oldest else None,
        "oldest_pending_mock_age_seconds": max(0, int((current - aware(oldest)).total_seconds())) if oldest else None,
        "last_event_at": last_event.isoformat() if last_event else None,
        "observed_at": current.isoformat(),
        "worker": snapshot(db),
        "note": "Mock notifications remain in outbox; there is no external delivery configured.",
    }


@router.get("/worker-health")
def worker_health(user: User = Depends(STAFF), db: Session = Depends(get_db)):
    """Staff-only heartbeat, independent of whether an SLA event was emitted."""
    return snapshot(db)
