"""Regression scenarios for FieldPulse v0.2.0."""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models import AdminAudit, Contract, Ticket, Worklog, SLAPolicy, now
from conftest import create_ticket, post, signin


def assigned(env):
    client, _, ids = env
    ca = signin(client, "a@test.local")
    t = create_ticket(client, ids, ca).json()
    cd = signin(client, "dispatcher@test.local")
    result = post(client, "/tickets/" + t["id"] + "/assign",
                  {"version": t["version"], "engineer_id": ids["engineer"]}, cd)
    assert result.status_code == 200, result.text
    ce = signin(client, "engineer@test.local")
    accepted = post(client, "/tickets/" + t["id"] + "/accept",
                    {"version": result.json()["version"]}, ce)
    assert accepted.status_code == 200, accepted.text
    return accepted.json(), ce


def action(ticket_id, version, key=None, summary="Checked wiring"):
    return {
        "uuid": key or str(uuid.uuid4()), "ticket_id": ticket_id,
        "base_version": version, "client_at": now().isoformat(),
        "summary": summary, "checklist": {"isolation": True},
        "materials": [{"name": "wire", "quantity": 2}], "photos": []
    }


def test_batch_applies_two_events_from_same_snapshot_without_external_rebase(env):
    client, Session, _ = env
    t, csrf = assigned(env)
    first, second = action(t["id"], t["version"]), action(t["id"], t["version"])
    reply = post(client, "/mobile/sync", {"actions": [first, second]}, csrf)
    assert reply.status_code == 200, reply.text
    assert [x["status"] for x in reply.json()["results"]] == ["applied", "applied"]
    with Session() as db:
        assert db.scalar(select(func.count(Worklog.id))) == 2
        assert db.get(Ticket, t["id"]).version == t["version"] + 2


def test_uuid_reuse_with_modified_payload_is_conflict(env):
    client, Session, _ = env
    t, csrf = assigned(env)
    original = action(t["id"], t["version"])
    assert post(client, "/mobile/sync", {"actions": [original]}, csrf).json()["results"][0]["status"] == "applied"
    modified = {**original, "summary": "Silent overwrite attempt"}
    reply = post(client, "/mobile/sync", {"actions": [modified]}, csrf)
    assert reply.json()["results"][0] == {
        "uuid": original["uuid"], "status": "conflict",
        "reason": "UUID_REUSED_WITH_DIFFERENT_PAYLOAD"
    }
    assert post(client, "/mobile/sync", {"actions": [original]}, csrf).json()["results"][0]["status"] == "duplicate"
    with Session() as db:
        assert db.scalar(select(func.count(Worklog.id))) == 1


def test_nonfinite_materials_are_invalid(env):
    client, Session, _ = env
    t, csrf = assigned(env)
    bad = action(t["id"], t["version"])
    bad["materials"] = [{"name": "test", "quantity": "NaN"}]
    reply = post(client, "/mobile/sync", {"actions": [bad]}, csrf)
    assert reply.status_code == 200
    assert reply.json()["results"][0]["reason"] == "INVALID_MATERIAL"
    with Session() as db:
        assert db.scalar(select(func.count(Worklog.id))) == 0


def test_admin_import_dry_run_does_not_persist_and_client_is_denied(env):
    client, Session, _ = env
    csv = ("organization,site_name,city,area,timezone\n"
           "New company,Second office,Moscow,north,Europe/Moscow\n").encode()
    cs = signin(client, "a@test.local")
    forbidden = client.post("/api/v1/admin/import/sites",
                            files={"file": ("sites.csv", csv, "text/csv")},
                            headers={"X-CSRF-Token": cs})
    assert forbidden.status_code == 403
    admin_csrf = signin(client, "admin@test.local")
    url = "/api/v1/admin/import/sites"
    args = {"files": {"file": ("sites.csv", csv, "text/csv")},
            "headers": {"X-CSRF-Token": admin_csrf}}
    checked = client.post(url + "?dry_run=true", **args)
    assert checked.status_code == 200, checked.text
    assert checked.json()["created_or_updated"] == 1
    with Session() as db:
        assert db.scalar(select(func.count(Ticket.id))) == 0
    committed = client.post(url, **args)
    assert committed.status_code == 200, committed.text
    assert committed.json()["created_or_updated"] == 1
    with Session() as db:
        assert db.scalar(select(func.count(AdminAudit.id))) == 1
    repeat = client.post(url, **args)
    assert repeat.status_code == 200
    assert repeat.json()["created_or_updated"] == 0


def test_admin_policy_revision_does_not_retroactively_modify_ticket(env):
    client, Session, ids = env
    ca = signin(client, "a@test.local")
    first = create_ticket(client, ids, ca).json()
    original = first["sla"]["reaction_minutes"]
    admin_csrf = signin(client, "admin@test.local")
    body = {
        "reaction_minutes": 45, "resolution_minutes": 400,
        "allowed_pauses": ["awaiting_access"],
        "week": {str(i): [["09:00", "18:00"]] for i in range(5)},
        "holidays": ["2026-11-16"]
    }
    resp = client.put("/api/v1/admin/contracts/"+ids["a_site"]+"/policies/P1",
                      json=body, headers={"X-CSRF-Token": admin_csrf})
    assert resp.status_code == 200, resp.text
    with Session() as db:
        old = db.get(Ticket, first["id"])
        assert old.policy_snapshot["reaction_minutes"] == original
        assert old.reaction_deadline is not None
        contract = db.scalar(select(Contract).where(Contract.site_id == ids["a_site"]))
        assert contract.policy_ids["P1"] != ids["policy"]
    ca = signin(client, "a@test.local")
    newer = post(client, "/tickets", {
        "site_id": ids["a_site"], "asset_id": ids["a_asset"],
        "priority": "P1", "kind": "inspection",
        "description": "Power supply requires second inspection"},
        ca, {"Idempotency-Key": str(uuid.uuid4())})
    assert newer.status_code == 201, newer.text
    assert newer.json()["sla"]["reaction_minutes"] == 45
    assert client.get("/api/v1/tickets/"+first["id"]).json()["sla"]["reaction_minutes"] == original


def test_admin_engineer_cannot_update_self_and_shift_overlap(env):
    client, Session, ids = env
    ce = signin(client, "engineer@test.local")
    update = client.put("/api/v1/admin/engineers/"+ids["engineer"]+"/profile",
                        json={"skills": ["hvac"], "area": "north", "active": False},
                        headers={"X-CSRF-Token": ce})
    assert update.status_code == 403
    admin_csrf = signin(client, "admin@test.local")
    changed = client.put("/api/v1/admin/engineers/"+ids["engineer"]+"/profile",
                         json={"skills": ["hvac"], "area": "east", "active": True},
                         headers={"X-CSRF-Token": admin_csrf})
    assert changed.status_code == 200, changed.text
    overlapping = client.post("/api/v1/admin/engineers/"+ids["engineer"]+"/shifts",
                              json={"starts_at": now().isoformat(),
                                    "ends_at": (now()+timedelta(hours=2)).isoformat()},
                              headers={"X-CSRF-Token": admin_csrf})
    assert overlapping.status_code == 409
    assert client.get("/api/v1/admin/audit").json()[0]["action"] == "engineer_profile_updated"


def test_working_minutes_used_in_reaction_report(env):
    client, Session, ids = env
    ca = signin(client, "a@test.local")
    t = create_ticket(client, ids, ca).json()
    zone = ZoneInfo("Europe/Moscow")
    with Session() as db:
        row = db.get(Ticket, t["id"])
        row.created_at = datetime(2026, 11, 13, 17, 50, tzinfo=zone).astimezone(timezone.utc)
        row.accepted_at = datetime(2026, 11, 16, 9, 20, tzinfo=zone).astimezone(timezone.utc)
        db.commit()
    signin(client, "dispatcher@test.local")
    report = client.get("/api/v1/reports/overview")
    assert report.status_code == 200, report.text
    assert report.json()["reaction_p50_minutes"] == 30
    assert report.json()["reaction_p95_minutes"] == 30


def test_uuid_payload_dictionary_key_order_is_ignored(env):
    client, Session, _ = env
    t, csrf = assigned(env)
    event = action(t["id"], t["version"])
    event["checklist"] = {"isolation": True, "voltage": True}
    first = post(client, "/mobile/sync", {"actions": [event]}, csrf)
    assert first.json()["results"][0]["status"] == "applied"
    equivalent = {**event, "checklist": {"voltage": True, "isolation": True}}
    reply = post(client, "/mobile/sync", {"actions": [equivalent]}, csrf)
    assert reply.status_code == 200
    assert reply.json()["results"][0]["status"] == "duplicate"
