"""PostgreSQL concurrency probes for assignment, offline UUIDs, acts and SLA locks.

Run inside the API container against a disposable seeded Compose stack.
"""
import base64
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from urllib.error import HTTPError
from urllib.request import Request

from PIL import Image
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Act, Attachment, Event, Outbox, Ticket, Worklog, now
from app.worker import run_once
from smoke import Browser


def request_any(browser, method, path, payload):
    headers = {"Content-Type": "application/json"}
    csrf = next((cookie.value for cookie in browser.jar if cookie.name == "fp_csrf"), "")
    headers["X-CSRF-Token"] = csrf
    req = Request(
        "http://127.0.0.1:8000/api/v1" + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers=headers, method=method
    )
    try:
        with browser.opener.open(req, timeout=30) as response:
            status, raw = response.status, response.read()
    except HTTPError as error:
        status, raw = error.code, error.read()
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw
    return status, body


def create_ticket(client, site, asset, description):
    return client.request("POST", "/tickets", {
        "site_id": site["id"], "asset_id": asset["id"], "priority": "P3",
        "kind": "repair", "description": description
    }, {"Idempotency-Key": uuid.uuid4().hex}, expected=201)


def race_two(left, right, path, payload_left, payload_right):
    barrier = Barrier(2)

    def send(browser, payload):
        barrier.wait(timeout=10)
        return request_any(browser, "POST", path, payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(send, left, payload_left)
        b = pool.submit(send, right, payload_right)
        return [a.result(), b.result()]


def check_assignment_cancel(client, dispatcher, engineer_id, site, asset):
    ticket = create_ticket(client, site, asset, "v0.8 concurrent assignment/cancel")
    dispatcher2 = Browser()
    dispatcher2.login("dispatcher@fieldpulse.local")
    path = "/tickets/" + ticket["id"] + "/"
    # Different endpoints share the same locked ticket and base version.
    barrier = Barrier(2)

    def send(browser, endpoint, payload):
        barrier.wait(timeout=10)
        return request_any(browser, "POST", path + endpoint, payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assigned = pool.submit(send, dispatcher, "assign",
                               {"version": ticket["version"], "engineer_id": engineer_id})
        cancelled = pool.submit(send, dispatcher2, "cancel",
                                {"version": ticket["version"]})
        outcomes = [assigned.result(), cancelled.result()]
    statuses = sorted(x[0] for x in outcomes)
    assert statuses == [200, 409], outcomes
    with SessionLocal() as db:
        current = db.get(Ticket, ticket["id"])
        events = db.scalars(select(Event).where(
            Event.ticket_id == ticket["id"],
            Event.type.in_(("assigned", "cancelled"))
        )).all()
        assert current.version == 2
        assert current.status in ("assigned", "cancelled")
        assert len(events) == 1
    return ticket["id"]


def start_two_tickets(client, dispatcher, site, asset, engineer_id):
    tickets = [
        create_ticket(client, site, asset, "v0.8 UUID race ticket A"),
        create_ticket(client, site, asset, "v0.8 UUID race ticket B"),
    ]
    # The assignment/cancel race above exercises the public assignment route.
    # These two rows are prepared directly so the UUID race can isolate two
    # independent ticket locks for the same engineer.
    with SessionLocal() as db:
        for item in tickets:
            row = db.get(Ticket, item["id"])
            row.assignee_id = engineer_id
            row.status = "in_progress"
            row.accepted_at = now()
            row.started_at = now()
            row.version += 2
        db.commit()
        return [(item["id"], db.get(Ticket, item["id"]).version) for item in tickets]


def check_mobile_uuid_race(client, dispatcher, site, asset, engineer_email, engineer_id):
    tickets = start_two_tickets(client, dispatcher, site, asset, engineer_id)
    engineer_a, engineer_b = Browser(), Browser()
    engineer_a.login(engineer_email)
    engineer_b.login(engineer_email)
    required = engineer_a.request("GET", "/tickets/" + tickets[0][0] + "/checklist")["required"]
    image = Image.new("RGB", (48, 48), (125, 75, 35))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    photo = base64.b64encode(buffer.getvalue()).decode("ascii")
    action_uuid = str(uuid.uuid4())
    actions = []
    for ticket_id, version in tickets:
        actions.append({
            "uuid": action_uuid, "ticket_id": ticket_id, "base_version": version,
            "client_at": now().isoformat(), "summary": "Concurrent UUID replay",
            "checklist": {key: True for key in required}, "materials": [],
            "photos": [{"name": "concurrency.jpg", "data": photo}],
        })
    outcomes = race_two(
        engineer_a, engineer_b, "/mobile/sync",
        {"actions": [actions[0]]}, {"actions": [actions[1]]}
    )
    results = [outcome[1]["results"][0] for outcome in outcomes]
    statuses = sorted(item["status"] for item in results)
    assert statuses == ["applied", "conflict"], outcomes
    winner = next(action for action, outcome in zip(actions, outcomes)
                  if outcome[1]["results"][0]["status"] == "applied")
    # The Worklog is globally unique per engineer/UUID even when ticket row
    # locks differ. A later replay of the winning payload is a duplicate.
    replay = engineer_a.request("POST", "/mobile/sync", {"actions": [winner]})
    assert replay["results"][0]["status"] == "duplicate", replay
    with SessionLocal() as db:
        logs = db.scalars(select(Worklog).where(
            Worklog.engineer_id == engineer_a.request("GET", "/auth/me")["id"],
            Worklog.action_uuid == action_uuid
        )).all()
        assert len(logs) == 1
        media = db.scalar(select(func.count(Attachment.id)).where(
            Attachment.worklog_id == logs[0].id))
        assert media == 1
        applied_ticket = logs[0].ticket_id
        current = db.get(Ticket, applied_ticket)
        assert current.version == 4
    return engineer_a, client, applied_ticket


def check_act_decision_race(engineer, client, ticket_id):
    current = engineer.request("GET", "/tickets/" + ticket_id)
    act = engineer.request("POST", "/tickets/" + ticket_id + "/submit",
                           {"version": current["version"]})
    assert act["status"] == "pending"
    manager = Browser()
    manager.login("manager@fieldpulse.local")
    client2 = Browser()
    client2.login("client-a@fieldpulse.local")
    outcomes = race_two(
        client2, manager, "/acts/" + act["id"] + "/decision",
        {"version": act["version"], "decision": "accepted", "reason": ""},
        {"version": act["version"], "decision": "rejected",
         "reason": "Concurrent review probe"}
    )
    assert sorted(item[0] for item in outcomes) == [200, 409], outcomes
    with SessionLocal() as db:
        row = db.get(Act, act["id"])
        events = db.scalars(select(Event).where(
            Event.ticket_id == ticket_id,
            Event.type.in_(("act_accepted", "act_rejected"))
        )).all()
        assert row.status in ("accepted", "rejected")
        assert len(events) == 1
        if row.status == "rejected":
            draft = db.scalar(select(Act).where(
                Act.ticket_id == ticket_id, Act.version == 2))
            assert draft is not None and draft.status == "draft"


def check_worker_locks(client, site, asset):
    ticket = create_ticket(client, site, asset, "v0.8 concurrent SLA worker lock")
    with SessionLocal() as db:
        row = db.get(Ticket, ticket["id"])
        row.reaction_deadline = now() - timedelta(minutes=15)
        row.resolution_deadline = now() - timedelta(minutes=15)
        row.accepted_at = None
        row.finished_at = None
        row.status = "new"
        db.commit()

    def scan():
        with SessionLocal() as db:
            return run_once(db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(scan)
        second = pool.submit(scan)
        first.result()
        second.result()
    with SessionLocal() as db:
        events = db.scalars(select(Event).where(
            Event.ticket_id == ticket["id"],
            Event.type.in_(("sla_reaction_breached", "sla_resolution_breached"))
        )).all()
        keys = {event.event_key for event in events}
        outbox = db.scalar(select(func.count(Outbox.id)).where(
            Outbox.event_key.in_(keys)
        )) if keys else 0
        assert len(events) == 2 and len(keys) == 2 and outbox == 2, (
            len(events), keys, outbox
        )


def main():
    client, dispatcher = Browser(), Browser()
    client.login("client-a@fieldpulse.local")
    dispatcher.login("dispatcher@fieldpulse.local")
    site = client.request("GET", "/sites")[0]
    asset = client.request("GET", "/assets?site_id=" + site["id"])[0]
    options = dispatcher.request("GET", "/engineers/suggestions?ticket_id=" +
                                 create_ticket(client, site, asset,
                                               "v0.8 eligibility probe")["id"])
    candidate = next((item for item in options if item["eligible"]), None)
    assert candidate, "No eligible engineer in the seeded demo environment"
    engineer_id = candidate["id"]
    engineer_number = int(candidate["name"].split()[-1])
    engineer_email = "engineer%d@fieldpulse.local" % engineer_number

    check_assignment_cancel(client, dispatcher, engineer_id, site, asset)
    engineer, client_session, ticket_id = check_mobile_uuid_race(
        client, dispatcher, site, asset, engineer_email, engineer_id
    )
    check_act_decision_race(engineer, client_session, ticket_id)
    check_worker_locks(client, site, asset)
    print("Concurrency probes: assignment/cancel, cross-ticket UUID, act decision and two SLA workers — OK")


if __name__ == "__main__":
    main()
