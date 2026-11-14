"""Crash-recoverable SLA scanner and local mock inbox worker.

One deterministic key per ticket, SLA stage and alert type. PostgreSQL row locks
and the unique event key prevent duplicate alerts after restarts.
"""
import logging
import time
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Event, Outbox, Pause, Ticket, now
from .api import emit, fmt
from .worker_health import record_cycle
from .sla import aware

logger = logging.getLogger("fieldpulse.worker")

def run_once(db: Session):
    current = now()
    count = 0
    warn_until = current + timedelta(minutes=10)
    q = select(Ticket).where(
        Ticket.status.notin_(("closed", "cancelled")),
        or_(
            and_(Ticket.accepted_at.is_(None), Ticket.reaction_deadline <= warn_until),
            and_(Ticket.finished_at.is_(None), Ticket.resolution_deadline <= warn_until),
        )
    ).with_for_update(skip_locked=True)
    for t in db.scalars(q):
        has_active_pause = db.scalar(select(Pause.id).where(
            Pause.ticket_id == t.id, Pause.ended_at.is_(None)).limit(1)) is not None
        checks = []
        if not t.accepted_at:
            checks.append(("reaction", t.reaction_deadline))
        if not has_active_pause and not t.finished_at:
            checks.append(("resolution", t.resolution_deadline))
        for stage, due in checks:
            due = aware(due)
            if current >= due:
                kind = "sla_" + stage + "_breached"
                key = "sla:%s:%s:breach" % (t.id, stage)
            elif current >= due - timedelta(minutes=10):
                kind = "sla_" + stage + "_warning"
                # After a justified pause changes the deadline, a new warning is valid.
                key = "sla:%s:%s:warning:%s" % (t.id, stage, due.isoformat())
            else:
                continue
            if db.scalar(select(Event.id).where(Event.event_key == key)):
                continue
            emit(db, t, kind, data={"deadline": fmt(due), "stage": stage}, key=key)
            db.add(Outbox(event_key=key, recipient="dispatcher@fieldpulse.local",
                          subject="%s: %s" % (kind, t.id[:8]),
                          body="Заявка %s, этап %s, срок %s" % (t.id, stage, fmt(due))))
            count += 1
    db.commit()
    return count

def main():
    logging.basicConfig(level="INFO")
    logger.info("FieldPulse worker started")
    while True:
        started_at = now()
        clock = time.perf_counter()
        try:
            with SessionLocal() as db:
                emitted = run_once(db)
            # Only a committed scan can be reported as a successful heartbeat.
            with SessionLocal() as health_db:
                record_cycle(health_db, started_at=started_at,
                             elapsed_ms=round((time.perf_counter()-clock)*1000),
                             emitted=emitted)
            if emitted:
                logger.info("SLA events created: %d", emitted)
        except Exception as error:
            logger.exception("SLA worker cycle failed; retrying")
            try:
                with SessionLocal() as health_db:
                    record_cycle(health_db, started_at=started_at,
                                 elapsed_ms=round((time.perf_counter()-clock)*1000),
                                 error=error)
            except Exception:
                logger.exception("SLA worker health recording failed")
        time.sleep(15)

if __name__ == "__main__":
    main()
