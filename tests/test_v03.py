"""Disaster recovery, media hygiene and SLA reporting tests."""
import hashlib
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.backup import FORMAT, verify_archive
from app.maintenance import candidates
from app.models import Event, Pause, Ticket, now
from app.worker import run_once
from conftest import create_ticket, post, signin


def build_archive(path, media=b"photo"):
    members = {
        "postgres/database.dump": b"database",
        "s3/tickets/" + "a" * 32 + "/" + "b" * 32 + ".jpg": media,
    }
    manifest = {
        "format": FORMAT, "created_at": now().isoformat(), "bucket": "fieldpulse",
        "members": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for name, content in members.items()
        }
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
        z.writestr("manifest.json", json.dumps(manifest))
    return members


def test_backup_manifest_verifies_and_detects_corruption(tmp_path):
    path = tmp_path / "backup.zip"
    items = build_archive(path)
    ok = verify_archive(path)
    assert ok["valid"] and ok["s3_objects"] == 1
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("postgres/database.dump", b"altered")
        for name, content in items.items():
            if name != "postgres/database.dump":
                z.writestr(name, content)
        manifest = {
            "format": FORMAT, "members": {
                name: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in items.items()
            }
        }
        z.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="Checksum mismatch"):
        verify_archive(path)


def test_orphan_scanner_respects_grace_period_and_references():
    key_a = "tickets/" + "a" * 32 + "/" + "b" * 32 + ".jpg"
    key_b = "tickets/" + "a" * 32 + "/" + "c" * 32 + "-thumb.jpg"
    old = now() - timedelta(hours=30)
    fresh = now() - timedelta(minutes=5)
    objs = [
        {"Key": key_a, "LastModified": old},
        {"Key": key_b, "LastModified": old},
        {"Key": "tickets/" + "a" * 32 + "/" + "d" * 32 + ".jpg",
         "LastModified": fresh},
        {"Key": "unknown/file", "LastModified": old},
    ]
    assert candidates(objs, {key_b}, now() - timedelta(hours=24)) == [key_a]


def test_reports_are_staff_only_and_reconcile_uses_snapshot(env):
    client, Session, ids = env
    ca = signin(client, "a@test.local")
    t = create_ticket(client, ids, ca).json()
    assert client.get("/api/v1/reports/sites").status_code == 403
    signin(client, "dispatcher@test.local")
    result = client.get("/api/v1/reports/sites")
    assert result.status_code == 200
    record = next(x for x in result.json()["sites"] if x["site_id"] == ids["a_site"])
    assert record["total"] == 1
    assert record["reaction_p50_minutes"] is None
    recon = client.get("/api/v1/reports/tickets/" + t["id"] + "/reconcile")
    assert recon.status_code == 200
    assert recon.json()["snapshot_policy"] == "P1"
    assert recon.json()["reaction_deadline"] == t["reaction_deadline"]
    assert any(e["type"] == "ticket_created" for e in recon.json()["timeline"])
    assert client.get("/api/v1/reports/operations").status_code == 200


def test_worker_reentrant_sessions_emit_only_one_breach_per_stage(env):
    client, Session, ids = env
    ca = signin(client, "a@test.local")
    t = create_ticket(client, ids, ca).json()
    with Session() as db:
        row = db.get(Ticket, t["id"])
        row.reaction_deadline = now() - timedelta(minutes=15)
        row.resolution_deadline = now() - timedelta(minutes=15)
        db.commit()
    with Session() as db:
        assert run_once(db) == 2
    # Simulate the worker restarting with a fresh DB session.
    with Session() as db:
        assert run_once(db) == 0
        events = db.scalars(select(Event).where(Event.ticket_id == t["id"],
            Event.type.in_(("sla_reaction_breached", "sla_resolution_breached")))).all()
        assert len(events) == 2


def test_new_sla_deadline_gets_new_warning_after_valid_pause(env):
    client, Session, ids = env
    ca = signin(client, "a@test.local")
    t = create_ticket(client, ids, ca).json()
    with Session() as db:
        row = db.get(Ticket, t["id"])
        row.accepted_at = now()
        row.resolution_deadline = now() + timedelta(minutes=6)
        db.commit()
        assert run_once(db) == 1
    with Session() as db:
        row = db.get(Ticket, t["id"])
        row.resolution_deadline = row.resolution_deadline + timedelta(minutes=2)
        db.commit()
        assert run_once(db) == 1
        assert run_once(db) == 0
        events = db.scalars(select(Event).where(
            Event.ticket_id == t["id"], Event.type == "sla_resolution_warning")).all()
        assert len(events) == 2


def test_active_pause_suppresses_resolution_alert(env):
    client, Session, ids = env
    ca = signin(client, "a@test.local")
    t = create_ticket(client, ids, ca).json()
    with Session() as db:
        ticket = db.get(Ticket, t["id"])
        ticket.accepted_at = now()
        ticket.resolution_deadline = now() - timedelta(minutes=5)
        db.add(Pause(ticket_id=t["id"], reason="awaiting_access",
                     actor_id=ids["engineer"], started_at=now() - timedelta(minutes=20)))
        db.commit()
    with Session() as db:
        assert run_once(db) == 0
