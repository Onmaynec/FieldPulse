"""SLA worker restart probe against an isolated seeded Compose environment."""
import argparse
import time
from datetime import timedelta

from sqlalchemy import func, select
from app.db import SessionLocal
from app.models import Event, Outbox, Ticket, now

TYPES = ("sla_reaction_breached", "sla_resolution_breached")

def count(db, ticket_id):
    events = db.scalars(select(Event).where(Event.ticket_id == ticket_id, Event.type.in_(TYPES))).all()
    return len(events), len({e.event_key for e in events})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "await", "verify"))
    parser.add_argument("ticket_id", nargs="?")
    args = parser.parse_args()
    if args.stage == "prepare":
        with SessionLocal() as db:
            ticket = db.scalar(select(Ticket).where(Ticket.kind == "load_profile").order_by(Ticket.created_at.desc()).limit(1))
            if ticket is None:
                raise RuntimeError("Seed load_profile tickets first")
            ticket.reaction_deadline = now() - timedelta(minutes=10)
            ticket.resolution_deadline = now() - timedelta(minutes=10)
            ticket.accepted_at = None
            ticket.finished_at = None
            ticket.status = "new"
            db.commit()
            print(ticket.id)
        return
    if not args.ticket_id:
        parser.error("ticket_id required")
    for _ in range(60):
        with SessionLocal() as db:
            n, unique = count(db, args.ticket_id)
            if n > 2 or n != unique:
                raise AssertionError("Duplicate SLA event detected")
            if n == 2:
                if args.stage == "verify":
                    events = db.scalars(select(Event).where(Event.ticket_id == args.ticket_id, Event.type.in_(TYPES))).all()
                    keys = [x.event_key for x in events]
                    outbox = db.scalar(select(func.count(Outbox.id)).where(Outbox.event_key.in_(keys)))
                    assert outbox == 2, (keys, outbox)
                print("SLA worker %s: exactly two distinct breaches — OK" % args.stage)
                return
        time.sleep(1)
    raise AssertionError("Worker did not emit expected events within 60 seconds")

if __name__ == "__main__":
    main()
