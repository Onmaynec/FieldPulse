"""Independent SLA-worker watchdog with durable incidents and optional HTTPS delivery.

The monitor itself is a separate Compose service. Its SQLite-compatible
business logic is tested without network. Only operator-configured HTTPS
webhook URLs can receive notices; ticket details or exception text never leave.
"""
import argparse
import ipaddress
import json
import logging
import os
import sys
import time
from datetime import timedelta
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import IncidentDelivery, MonitorCycle, OperationalIncident, now
from .sla import aware
from .worker_health import snapshot

logger = logging.getLogger("fieldpulse.monitor")
SOURCE = "sla"
INTERVAL_SECONDS = 15
INITIAL_GRACE_SECONDS = 90
MONITOR_STALE_SECONDS = 90
MAX_RETRY_SECONDS = 900


def webhook_url(raw: str) -> str:
    """An explicit admin-supplied HTTPS endpoint, never an HTTP API parameter."""
    if not raw:
        return ""
    try:
        p = urlsplit(raw)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError("HTTPS URL required; userinfo and fragments are forbidden")
        # Reject literal private/loopback/link-local IP targets. DNS is under
        # control of the deployment's operator, not a browser or client.
        try:
            if not ipaddress.ip_address(p.hostname).is_global:
                raise ValueError("Webhook IP must be public")
        except ValueError as err:
            if "Webhook IP" in str(err):
                raise
        if len(raw) > 2048:
            raise ValueError("Webhook URL is too long")
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid ALERT_WEBHOOK_URL (HTTPS public endpoint required)") from error
    return raw


def monitor_snapshot(db: Session, *, at=None):
    instant = aware(at or now())
    state = db.get(MonitorCycle, "watchdog")
    if not state or not state.last_success_at:
        return {"state": "unknown", "last_success_at": None, "seconds_since_success": None,
                "stale_after_seconds": MONITOR_STALE_SECONDS}
    age = max(0, int((instant-aware(state.last_success_at)).total_seconds()))
    return {"state": "healthy" if age <= MONITOR_STALE_SECONDS else "stale",
            "last_success_at": aware(state.last_success_at).isoformat(),
            "seconds_since_success": age, "stale_after_seconds": MONITOR_STALE_SECONDS}


def monitor_tick(db: Session, *, at=None, started_at=None, webhook_enabled=False):
    """Open/update/close one incident for the worker; return a short outcome.

    Unknown states have a 90-second startup grace. Existing incidents are
    closed only after a confirmed healthy worker, never on transient unknown.
    """
    current = aware(at or now())
    started_at = aware(started_at or current)
    worker = snapshot(db, at=current)
    problem = worker["state"] in ("stale", "error") or (
        worker["state"] == "unknown" and (current-started_at).total_seconds() >= INITIAL_GRACE_SECONDS)
    existing = db.scalar(select(OperationalIncident).where(
        OperationalIncident.source == SOURCE,
        OperationalIncident.recovered_at.is_(None)).with_for_update())
    outcome = "unchanged"
    if problem:
        if existing is None:
            incident = OperationalIncident(source=SOURCE, state=worker["state"],
                                           opened_at=current, last_seen_at=current)
            db.add(incident)
            db.flush()
            if webhook_enabled:
                db.add(IncidentDelivery(incident_id=incident.id, event="opened",
                                        created_at=current, next_attempt_at=current))
            outcome = "opened"
        else:
            existing.state = worker["state"]
            existing.last_seen_at = current
    elif worker["state"] == "healthy" and existing is not None:
        existing.recovered_at = current
        existing.last_seen_at = current
        if webhook_enabled:
            db.add(IncidentDelivery(incident_id=existing.id, event="recovered",
                                    created_at=current, next_attempt_at=current))
        outcome = "recovered"
    heartbeat = db.get(MonitorCycle, "watchdog")
    if heartbeat is None:
        heartbeat = MonitorCycle(id="watchdog", last_success_at=current, cycles=0)
        db.add(heartbeat)
    heartbeat.last_success_at = current
    heartbeat.cycles += 1
    db.commit()
    return {"result": outcome, "worker_state": worker["state"]}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_webhook(url, payload):
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json",
                               "User-Agent": "FieldPulse-operations/0.7"},
                      method="POST")
    with build_opener(_NoRedirect()).open(request, timeout=5) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("Webhook rejected message")
    return True


def deliver_pending(db: Session, url: str, *, at=None, sender=send_webhook, limit=5):
    """Retry via a separate transaction. Event receivers must deduplicate keys."""
    if not url:
        return {"attempted": 0, "sent": 0}
    current = aware(at or now())
    rows = db.scalars(select(IncidentDelivery).where(
        IncidentDelivery.sent_at.is_(None),
        IncidentDelivery.next_attempt_at <= current)
        .order_by(IncidentDelivery.created_at.asc(), IncidentDelivery.id.asc())
        .with_for_update(skip_locked=True).limit(limit)).all()
    success = 0
    for delivery in rows:
        incident = db.get(OperationalIncident, delivery.incident_id)
        # An outbox record has only bounded metadata and no ticket/user content.
        payload = {
            "event": "fieldpulse.worker." + delivery.event,
            "idempotency_key": delivery.incident_id + ":" + delivery.event,
            "incident_id": delivery.incident_id, "source": SOURCE,
            "state": incident.state if delivery.event == "opened" else "recovered",
            "occurred_at": (aware(incident.opened_at) if delivery.event == "opened"
                            else aware(incident.recovered_at)).isoformat(),
        }
        delivery.attempts += 1
        try:
            sender(url, payload)
            delivery.sent_at = current
            delivery.last_error_type = None
            success += 1
        except Exception as error:
            # Keep only the exception TYPE; never store a URL or response body.
            delivery.last_error_type = type(error).__name__[:80]
            delay = min(15 * 2**min(delivery.attempts, 6), MAX_RETRY_SECONDS)
            delivery.next_attempt_at = current + timedelta(seconds=delay)
            logger.warning("Webhook delivery failed: %s, retry scheduled", type(error).__name__)
        db.commit()
    return {"attempted": len(rows), "sent": success}


def main():
    parser = argparse.ArgumentParser(description="FieldPulse independent SLA monitor")
    parser.add_argument("--check", action="store_true", help="fail if monitor stopped cycling")
    args = parser.parse_args()
    if args.check:
        with SessionLocal() as db:
            health = monitor_snapshot(db)
        print("FieldPulse monitor: " + health["state"])
        if health["state"] != "healthy":
            sys.exit(1)
        return
    url = webhook_url(os.getenv("ALERT_WEBHOOK_URL", "").strip())
    logging.basicConfig(level="INFO")
    started = now()
    logger.info("Independent FieldPulse watchdog started; webhook enabled: %s", bool(url))
    while True:
        try:
            with SessionLocal() as db:
                result = monitor_tick(db, started_at=started, webhook_enabled=bool(url))
            if result["result"] != "unchanged":
                logger.warning("SLA worker incident %s (state=%s)",
                               result["result"], result["worker_state"])
            if url:
                with SessionLocal() as db:
                    deliver_pending(db, url)
        except Exception:
            logger.exception("Monitor cycle failed; retrying")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
