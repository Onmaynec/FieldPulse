"""Authenticated operational incident review and acknowledgements."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import IncidentDelivery, OperationalIncident, User, now
from .monitor import monitor_snapshot
from .security import roles
from .sla import aware

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])
STAFF = roles("admin", "manager", "dispatcher")


def json_incident(item):
    return {"id": item.id, "source": item.source, "state": item.state,
            "opened_at": aware(item.opened_at).isoformat(),
            "last_seen_at": aware(item.last_seen_at).isoformat(),
            "recovered_at": aware(item.recovered_at).isoformat() if item.recovered_at else None,
            "acknowledged_at": aware(item.acknowledged_at).isoformat() if item.acknowledged_at else None,
            "acknowledged": bool(item.acknowledged_at)}


@router.get("/incidents")
def incidents(limit: int = Query(20, ge=1, le=100), active_only: bool = False,
              user: User = Depends(STAFF), db: Session = Depends(get_db)):
    query = select(OperationalIncident)
    if active_only:
        query = query.where(OperationalIncident.recovered_at.is_(None))
    rows = db.scalars(query.order_by(OperationalIncident.opened_at.desc(),
                                    OperationalIncident.id.desc()).limit(limit)).all()
    return [json_incident(row) for row in rows]


@router.post("/incidents/{incident_id}/ack")
def acknowledge(incident_id: str, user: User = Depends(STAFF),
                db: Session = Depends(get_db)):
    incident = db.scalar(select(OperationalIncident)
                         .where(OperationalIncident.id == incident_id)
                         .with_for_update())
    if incident is None:
        raise HTTPException(404, detail="Инцидент не найден")
    if incident.acknowledged_at is None:
        incident.acknowledged_at = now()
        incident.acknowledged_by_id = user.id
        db.commit()
    return json_incident(incident)


@router.get("/summary")
def summary(user: User = Depends(STAFF), db: Session = Depends(get_db)):
    active = db.scalar(select(func.count(OperationalIncident.id)).where(
        OperationalIncident.recovered_at.is_(None))) or 0
    unacknowledged = db.scalar(select(func.count(OperationalIncident.id)).where(
        OperationalIncident.recovered_at.is_(None),
        OperationalIncident.acknowledged_at.is_(None))) or 0
    pending = db.scalar(select(func.count(IncidentDelivery.id)).where(
        IncidentDelivery.sent_at.is_(None))) or 0
    sent = db.scalar(select(func.count(IncidentDelivery.id)).where(
        IncidentDelivery.sent_at.is_not(None))) or 0
    failed = db.scalar(select(func.count(IncidentDelivery.id)).where(
        IncidentDelivery.sent_at.is_(None), IncidentDelivery.attempts > 0)) or 0
    return {"active_incidents": active, "unacknowledged": unacknowledged,
            "pending_webhook_deliveries": pending,
            "retrying_webhook_deliveries": failed, "sent_webhook_deliveries": sent,
            "monitor": monitor_snapshot(db), "observed_at": now().isoformat()}
