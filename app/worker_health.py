"""Persistent worker-cycle telemetry, kept separate from SLA processing.

Do not include exception messages, ticket ids, user ids or payloads in telemetry.
A successful cycle is recorded only AFTER run_once has committed.
"""
import argparse
import sys
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import WorkerCycle, now
from .sla import aware

WORKER_ID = "sla"
HEALTH_THRESHOLD_SECONDS = 75


def record_cycle(db: Session, *, started_at: datetime, elapsed_ms: int,
                 emitted: int = 0, error: BaseException | None = None):
    state = db.scalar(select(WorkerCycle).where(
        WorkerCycle.id == WORKER_ID).with_for_update())
    if state is None:
        state = WorkerCycle(id=WORKER_ID, last_emitted=0, cycles=0, failures=0)
        db.add(state)
    state.last_started_at = started_at
    state.last_duration_ms = max(0, elapsed_ms)
    state.cycles += 1
    if error is None:
        state.last_success_at = now()
        state.last_emitted = emitted
        state.last_error_type = None
    else:
        state.last_failure_at = now()
        state.failures += 1
        state.last_error_type = type(error).__name__[:80]
    db.commit()


def snapshot(db: Session, *, at=None, threshold_seconds=HEALTH_THRESHOLD_SECONDS):
    at = aware(at or now())
    row = db.get(WorkerCycle, WORKER_ID)
    result = {
        "state": "unknown", "last_started_at": None, "last_success_at": None,
        "last_failure_at": None, "last_duration_ms": None,
        "last_emitted": 0, "cycles": 0, "failures": 0,
        "last_error_type": None, "seconds_since_success": None,
        "stale_after_seconds": threshold_seconds,
    }
    if row is None:
        return result
    age = max(0, int((at - aware(row.last_success_at)).total_seconds())) if row.last_success_at else None
    error_latest = row.last_failure_at is not None and (
        row.last_success_at is None or aware(row.last_failure_at) >= aware(row.last_success_at))
    state = ("error" if error_latest else
             "unknown" if age is None else
             "stale" if age > threshold_seconds else "healthy")
    result.update({
        "state": state,
        "last_started_at": aware(row.last_started_at).isoformat() if row.last_started_at else None,
        "last_success_at": aware(row.last_success_at).isoformat() if row.last_success_at else None,
        "last_failure_at": aware(row.last_failure_at).isoformat() if row.last_failure_at else None,
        "last_duration_ms": row.last_duration_ms, "last_emitted": row.last_emitted,
        "cycles": row.cycles, "failures": row.failures,
        "last_error_type": row.last_error_type,
        "seconds_since_success": age,
    })
    return result


def main():
    parser = argparse.ArgumentParser(description="FieldPulse worker health probe")
    parser.add_argument("--check", action="store_true", help="exit 1 if worker is not healthy")
    args = parser.parse_args()
    from .db import SessionLocal
    with SessionLocal() as db:
        result = snapshot(db)
    print("FieldPulse SLA worker: " + result["state"] +
          " (age=" + str(result["seconds_since_success"]) + "s)")
    if args.check and result["state"] != "healthy":
        sys.exit(1)


if __name__ == "__main__":
    main()
