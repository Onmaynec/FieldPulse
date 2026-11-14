"""v0.7: independent SLA monitoring, privacy, acknowledgement and retry delivery."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import IncidentDelivery, MonitorCycle, OperationalIncident, WorkerCycle, now
from app.monitor import (deliver_pending, monitor_snapshot, monitor_tick,
                         webhook_url)
from app.worker_health import record_cycle
from conftest import signin


def test_monitor_grace_open_idempotence_recover_reopen(env):
    client, Session, ids = env
    current = now()
    with Session() as db:
        grace = monitor_tick(db, at=current, started_at=current - timedelta(seconds=20))
        assert grace["result"] == "unchanged"
        assert db.scalar(select(func.count(OperationalIncident.id))) == 0
        opened = monitor_tick(db, at=current, started_at=current - timedelta(seconds=95))
        assert opened == {"result": "opened", "worker_state": "unknown"}
        again = monitor_tick(db, at=current + timedelta(seconds=3),
                             started_at=current - timedelta(seconds=95))
        assert again["result"] == "unchanged"
        incidents = db.scalars(select(OperationalIncident)).all()
        assert len(incidents) == 1
        original_id = incidents[0].id
        assert incidents[0].acknowledged_at is None
        record_cycle(db, started_at=current, elapsed_ms=12, emitted=0)
        recovered = monitor_tick(db, at=now())
        assert recovered == {"result": "recovered", "worker_state": "healthy"}
        assert db.get(OperationalIncident, original_id).recovered_at is not None
        failed = RuntimeError("PRIVATE DATA MUST NOT LEAK")
        record_cycle(db, started_at=now(), elapsed_ms=32, error=failed)
        reopened = monitor_tick(db, at=now())
        assert reopened == {"result": "opened", "worker_state": "error"}
        assert db.scalar(select(func.count(OperationalIncident.id))) == 2
        open_items = db.scalars(select(OperationalIncident).where(
            OperationalIncident.recovered_at.is_(None))).all()
        assert len(open_items) == 1
        assert open_items[0].id != original_id
        assert "PRIVATE DATA" not in str(open_items[0].state)


def test_monitor_stale_signal_and_recovery(env):
    client, Session, ids = env
    with Session() as db:
        record_cycle(db, started_at=now(), elapsed_ms=14)
        row = db.get(WorkerCycle, "sla")
        row.last_success_at = now() - timedelta(seconds=100)
        db.commit()
        stale = monitor_tick(db, at=now())
        assert stale["result"] == "opened" and stale["worker_state"] == "stale"
        record_cycle(db, started_at=now(), elapsed_ms=18)
        assert monitor_tick(db, at=now())["result"] == "recovered"
        assert db.scalar(select(func.count(OperationalIncident.id))) == 1
        assert db.scalar(select(func.count(OperationalIncident.id)).where(
            OperationalIncident.recovered_at.is_(None))) == 0


def test_webhook_retries_and_recovery_notice_no_private_fields(env):
    client, Session, ids = env
    now_at = now()
    sent = []
    with Session() as db:
        assert monitor_tick(db, at=now_at,
                            started_at=now_at - timedelta(seconds=100),
                            webhook_enabled=True)["result"] == "opened"
        assert db.scalar(select(func.count(IncidentDelivery.id))) == 1
        def refuse(url, payload):
            assert url == "https://alerts.example.net/receiver"
            raise TimeoutError("SECRET: https://alerts.example.net/hook-private")
        bad = deliver_pending(db, "https://alerts.example.net/receiver",
                              at=now_at, sender=refuse)
        assert bad == {"attempted": 1, "sent": 0}
        row = db.scalar(select(IncidentDelivery))
        assert row.attempts == 1 and row.sent_at is None
        assert row.last_error_type == "TimeoutError"
        assert row.next_attempt_at > now_at
        assert "SECRET:" not in str(row.last_error_type)
        assert deliver_pending(db, "https://alerts.example.net/receiver",
                               at=now_at + timedelta(seconds=1),
                               sender=lambda *a: sent.append(a))["attempted"] == 0
        def accept(url, payload):
            sent.append(payload)
            return True
        good = deliver_pending(db, "https://alerts.example.net/receiver",
                               at=now_at + timedelta(seconds=31), sender=accept)
        assert good == {"attempted": 1, "sent": 1}
        assert sent[0]["event"] == "fieldpulse.worker.opened"
        assert all(k not in sent[0] for k in ("ticket_id", "user_id", "description", "exception"))
        assert len(sent[0]["idempotency_key"]) > 32
        assert deliver_pending(db, "https://alerts.example.net/receiver",
                               at=now_at + timedelta(seconds=35),
                               sender=accept)["attempted"] == 0
        record_cycle(db, started_at=now(), elapsed_ms=5)
        assert monitor_tick(db, at=now(), webhook_enabled=True)["result"] == "recovered"
        assert db.scalar(select(func.count(IncidentDelivery.id))) == 2
        done = deliver_pending(db, "https://alerts.example.net/receiver",
                               at=now() + timedelta(seconds=5), sender=accept)
        assert done == {"attempted": 1, "sent": 1}
        assert sent[1]["event"] == "fieldpulse.worker.recovered"
        assert sent[1]["incident_id"] == sent[0]["incident_id"]
        assert sent[1]["idempotency_key"] != sent[0]["idempotency_key"]


def test_incident_api_permissions_csrf_and_idempotent_ack(env):
    client, Session, ids = env
    with Session() as db:
        t = now()
        monitor_tick(db, at=t, started_at=t - timedelta(seconds=100))
        incident_id = db.scalar(select(OperationalIncident)).id
    signin(client, "a@test.local")
    for path in ("/api/v1/operations/incidents", "/api/v1/operations/summary"):
        assert client.get(path).status_code == 403
    assert client.post("/api/v1/operations/incidents/"+incident_id+"/ack",
                       headers={"X-CSRF-Token":client.cookies["fp_csrf"]}).status_code == 403
    signin(client, "dispatcher@test.local")
    assert client.get("/api/v1/operations/incidents").json()[0]["id"] == incident_id
    stat = client.get("/api/v1/operations/summary").json()
    assert stat["active_incidents"] == 1 and stat["unacknowledged"] == 1
    assert stat["pending_webhook_deliveries"] == 0
    assert client.post("/api/v1/operations/incidents/"+incident_id+"/ack").status_code == 403
    ack = client.post("/api/v1/operations/incidents/"+incident_id+"/ack",
                      headers={"X-CSRF-Token":client.cookies["fp_csrf"]})
    assert ack.status_code == 200 and ack.json()["acknowledged"] is True
    stamp = ack.json()["acknowledged_at"]
    repeat = client.post("/api/v1/operations/incidents/"+incident_id+"/ack",
                         headers={"X-CSRF-Token":client.cookies["fp_csrf"]})
    assert repeat.status_code == 200 and repeat.json()["acknowledged_at"] == stamp
    assert client.get("/api/v1/operations/summary").json()["unacknowledged"] == 0
    missing = client.post("/api/v1/operations/incidents/" + ("0"*32) + "/ack",
                          headers={"X-CSRF-Token":client.cookies["fp_csrf"]})
    assert missing.status_code == 404
    assert client.get("/api/v1/operations/incidents?limit=0").status_code == 422
    assert client.get("/api/v1/operations/incidents?limit=101").status_code == 422
    signin(client, "engineer@test.local")
    assert client.get("/api/v1/operations/incidents").status_code == 403


def test_monitor_heartbeat_can_be_detected_as_stale(env):
    client, Session, ids = env
    moment = now()
    with Session() as db:
        assert monitor_snapshot(db)["state"] == "unknown"
        monitor_tick(db, at=moment, started_at=moment)
        health = monitor_snapshot(db, at=moment + timedelta(seconds=5))
        assert health["state"] == "healthy"
        assert health["seconds_since_success"] == 5
        assert monitor_snapshot(db, at=moment+timedelta(seconds=95))["state"] == "stale"
        assert db.get(MonitorCycle, "watchdog").cycles == 1


@pytest.mark.parametrize("value", [
    "http://alerts.example.net/receiver", "https://127.0.0.1/hook",
    "https://10.0.0.2/hook", "https://user:secret@example.net/hook",
    "https://example.net/path#fragment", "file:///etc/passwd",
    "https://", "not-an-url",
])
def test_untrusted_webhook_urls_fail_closed(value):
    with pytest.raises(ValueError):
        webhook_url(value)


def test_webhook_accepts_https_and_disabled_default():
    assert webhook_url("") == ""
    assert webhook_url("https://alerts.example.net/receiver") == "https://alerts.example.net/receiver"
