"""v0.6: signed cursors, SQL dashboard scopes, worker heartbeat diagnostics."""
from datetime import timedelta
from urllib.parse import urlencode

from sqlalchemy import select

from app.models import Pause, Site, Ticket, WorkerCycle, now, uid
from app.registry_seek import SIGNER
from app.worker_health import record_cycle, snapshot
from conftest import signin
from test_v05 import populate


def collect_seek(client, *, sort="newest", filters=None):
    params = {"page_size": 10, "sort": sort, **(filters or {})}
    seen = []
    tokens = set()
    while True:
        resp = client.get("/api/v1/tickets/seek", params=params)
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert "total" not in result
        assert result["mode"] == "keyset"
        seen.extend(x["id"] for x in result["items"])
        if not result["has_more"]:
            assert result["next_cursor"] is None
            break
        token = result["next_cursor"]
        assert token and token not in tokens
        tokens.add(token)
        params["cursor"] = token
        assert len(tokens) <= 30, "Cursor cycle"
    return seen


def test_keyset_matches_offset_across_all_sort_orders(env):
    client, Session, ids = env
    populate(env)
    signin(client, "a@test.local")
    for sort in ("newest", "deadline", "priority"):
        seen = collect_seek(client, sort=sort)
        page = client.get("/api/v1/tickets/search",
                          params={"page_size": 100, "sort": sort}).json()
        assert len(seen) == len(set(seen)) == page["total"] == 29
        assert seen == [row["id"] for row in page["items"]]
    p1 = collect_seek(client, sort="priority", filters={"priority": "P1"})
    offset = client.get("/api/v1/tickets/search",
                        params={"page_size": 100, "sort": "priority",
                                "priority": "P1"}).json()
    assert p1 == [item["id"] for item in offset["items"]]
    assert len(p1) > 10


def test_cursor_cannot_be_replayed_or_tampered_across_users_and_filters(env):
    client, _, ids = env
    populate(env)
    signin(client, "a@test.local")
    start = client.get("/api/v1/tickets/seek?page_size=10").json()
    token = start["next_cursor"]
    assert token
    bad = token[:-3] + ("xyz" if token[-3:] != "xyz" else "abc")
    assert client.get("/api/v1/tickets/seek",
                      params={"cursor": bad, "page_size": 10}).status_code == 422
    assert client.get("/api/v1/tickets/seek",
                      params={"cursor": token, "page_size": 20}).status_code == 422
    assert client.get("/api/v1/tickets/seek",
                      params={"cursor": token, "page_size": 10, "sort": "deadline"}).status_code == 422
    assert client.get("/api/v1/tickets/seek",
                      params={"cursor": token, "page_size": 10, "status": "new"}).status_code == 422
    assert client.get("/api/v1/tickets/seek?page_size=0").status_code == 422
    assert client.get("/api/v1/tickets/seek?priority=P9").status_code == 422
    assert client.get("/api/v1/tickets/seek?sort=arbitrary").status_code == 422
    # Even another authenticated user of the same organization cannot reuse a cursor.
    signin(client, "b@test.local")
    assert client.get("/api/v1/tickets/seek",
                      params={"cursor": token, "page_size": 10}).status_code == 422
    assert client.get("/api/v1/tickets/seek").json()["items"] == []
    signin(client, "dispatcher@test.local")
    assert client.get("/api/v1/tickets/seek",
                      params={"cursor": token, "page_size": 10}).status_code == 422


def test_cursor_can_reject_wrong_signature_even_with_matching_payload(env):
    client, _, _ = env
    populate(env)
    signin(client, "a@test.local")
    p = client.get("/api/v1/tickets/seek?page_size=10").json()["next_cursor"]
    payload = SIGNER.loads(p)
    payload["h"] = "different"
    changed = SIGNER.dumps(payload)
    assert client.get("/api/v1/tickets/seek",
                      params={"cursor": changed, "page_size": 10}).status_code == 422


def test_dashboard_counts_all_tickets_and_isolates_clients_and_engineers(env):
    client, Session, ids = env
    seeded = populate(env)
    with Session() as db:
        original = db.get(Ticket, seeded["id"])
        original.resolution_deadline = now() - timedelta(minutes=10)
        # A different active ticket is paused and must not count as overdue.
        paused = db.scalar(select(Ticket).where(
            Ticket.id != original.id, Ticket.priority == "P1"))
        paused.resolution_deadline = now() - timedelta(minutes=8)
        db.add(Pause(ticket_id=paused.id, actor_id=ids["engineer"],
                     reason="awaiting_access", started_at=now() - timedelta(minutes=9)))
        b_site = db.get(Site, ids["b_site"])
        db.add(Ticket(
            id=uid(), org_id=b_site.org_id, site_id=b_site.id,
            asset_id=ids["b_asset"], creator_id=original.creator_id,
            priority="P1", kind=original.kind, description="Foreign private ticket",
            status="new", policy_snapshot=original.policy_snapshot,
            reaction_deadline=original.reaction_deadline,
            resolution_deadline=now()+timedelta(hours=2),
            created_at=now(),
        ))
        db.commit()
    signin(client, "a@test.local")
    a = client.get("/api/v1/board/summary")
    assert a.status_code == 200
    a = a.json()
    assert a["total"] == 29
    assert a["open"] == 29
    assert a["overdue"] == 1
    assert sum(a["by_status"].values()) == a["total"]
    assert len(a["upcoming"]) == 6
    assert len(a["recent"]) == 7
    assert not any(x["description"] == "Foreign private ticket" for x in a["recent"])
    assert all(x["site_id"] == ids["a_site"] for x in a["upcoming"])

    signin(client, "b@test.local")
    b = client.get("/api/v1/board/summary").json()
    assert b["total"] == 1
    assert b["recent"][0]["description"] == "Foreign private ticket"
    assert b["overdue"] == 0
    signin(client, "engineer@test.local")
    e = client.get("/api/v1/board/summary").json()
    assert e["total"] == 4
    assert all(x["assignee_id"] == ids["engineer"] for x in e["recent"])
    signin(client, "dispatcher@test.local")
    full = client.get("/api/v1/board/summary").json()
    assert full["total"] == 30
    assert full["open_p1"] >= 1


def test_worker_heartbeat_is_independent_from_sla_event_count(env):
    client, Session, ids = env
    signin(client, "a@test.local")
    assert client.get("/api/v1/reports/worker-health").status_code == 403
    signin(client, "dispatcher@test.local")
    unknown = client.get("/api/v1/reports/worker-health").json()
    assert unknown["state"] == "unknown"
    with Session() as db:
        record_cycle(db, started_at=now()-timedelta(milliseconds=20),
                     elapsed_ms=13, emitted=0)
    healthy = client.get("/api/v1/reports/worker-health").json()
    assert healthy["state"] == "healthy"
    assert healthy["cycles"] == 1
    assert healthy["last_emitted"] == 0
    with Session() as db:
        stale = snapshot(db, at=now()+timedelta(seconds=90))
    assert stale["state"] == "stale"
    with Session() as db:
        record_cycle(db, started_at=now(), elapsed_ms=44,
                     error=RuntimeError("private-ticket-12345"))
    failed = client.get("/api/v1/reports/worker-health").json()
    assert failed["state"] == "error" and failed["failures"] == 1
    assert failed["last_error_type"] == "RuntimeError"
    assert "private-ticket-12345" not in str(failed)
    with Session() as db:
        record_cycle(db, started_at=now(), elapsed_ms=27, emitted=1)
    recovered = client.get("/api/v1/reports/worker-health").json()
    assert recovered["state"] == "healthy" and recovered["cycles"] == 3
    ops = client.get("/api/v1/reports/operations").json()
    assert ops["worker"]["state"] == "healthy"


def test_full_registry_is_still_scoped_after_shared_query_refactor(env):
    client, _, ids = env
    populate(env)
    signin(client, "b@test.local")
    assert client.get("/api/v1/tickets/search").json()["total"] == 0
    assert client.get("/api/v1/tickets/seek").json()["items"] == []
    signin(client, "engineer@test.local")
    assert client.get("/api/v1/tickets/search").json()["total"] == 4
    assert len(collect_seek(client, sort="deadline")) == 4
