"""Server-side registry, personal saved views and operational metrics (v0.5)."""
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import select

from app.models import Ticket, TicketView, now, uid
from conftest import create_ticket, post, signin


def populate(env):
    client, Session, ids = env
    ca = signin(client, "a@test.local")
    seed = create_ticket(client, ids, ca).json()
    with Session() as db:
        template = db.get(Ticket, seed["id"])
        for i in range(28):
            db.add(Ticket(
                id=uid(), org_id=template.org_id, site_id=template.site_id,
                asset_id=template.asset_id, creator_id=template.creator_id,
                assignee_id=ids["engineer"] if i < 4 else None,
                priority="P1" if i % 2 else "P2",
                kind=template.kind,
                description="FilterNeedle-%02d" % i if i != 17 else "literal%_token",
                policy_snapshot=template.policy_snapshot,
                reaction_deadline=template.reaction_deadline + timedelta(hours=i),
                resolution_deadline=template.resolution_deadline + timedelta(hours=i),
                created_at=now() - timedelta(minutes=i+1),
                status="in_progress" if i < 4 else "new",
            ))
        db.commit()
    return seed


def test_server_pagination_cross_tenant_and_validation(env):
    client, Session, ids = env
    populate(env)
    result = client.get("/api/v1/tickets/search?page=1&page_size=10")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["total"] == 29 and data["pages"] == 3
    assert len(data["items"]) == 10
    first_ids = {item["id"] for item in data["items"]}
    second = client.get("/api/v1/tickets/search?page=2&page_size=10").json()
    assert second["total"] == 29
    assert not first_ids.intersection(item["id"] for item in second["items"])
    last = client.get("/api/v1/tickets/search?page=3&page_size=10").json()
    assert len(last["items"]) == 9
    outside = client.get("/api/v1/tickets/search?page=4&page_size=10").json()
    assert outside["items"] == [] and outside["total"] == 29
    assert client.get("/api/v1/tickets/search?page=0").status_code == 422
    assert client.get("/api/v1/tickets/search?page_size=101").status_code == 422
    assert client.get("/api/v1/tickets/search?sort=hacked").status_code == 422
    assert client.get("/api/v1/tickets/search?status=missing").status_code == 422
    assert client.get("/api/v1/tickets/search?priority=P9").status_code == 422
    assert client.get("/api/v1/tickets/search?q=" + ("x" * 101)).status_code == 422

    # A user from a separate organization cannot enumerate or search A's tickets.
    signin(client, "b@test.local")
    isolated = client.get("/api/v1/tickets/search?sort=priority").json()
    assert isolated["total"] == 0
    assert client.get("/api/v1/tickets/search?q=FilterNeedle").json()["total"] == 0
    signin(client, "engineer@test.local")
    assigned = client.get("/api/v1/tickets/search?active=true").json()
    assert assigned["total"] == 4
    assert all(item["assignee_id"] == ids["engineer"] for item in assigned["items"])
    assert client.get("/api/v1/tickets/search?status=closed").json()["total"] == 0
    signin(client, "dispatcher@test.local")
    assert client.get("/api/v1/tickets/search").json()["total"] == 29


def test_search_filters_match_literal_wildcards_and_stable_sort(env):
    client, _, _ = env
    populate(env)
    exact = client.get("/api/v1/tickets/search?q=literal%25_token").json()
    assert exact["total"] == 1
    assert exact["items"][0]["description"] == "literal%_token"
    by_name = client.get("/api/v1/tickets/search?q=Site%20A").json()
    assert by_name["total"] == 29
    by_priority = client.get("/api/v1/tickets/search?priority=P2&sort=priority&page_size=100").json()
    assert by_priority["total"] == 14
    assert all(item["priority"] == "P2" for item in by_priority["items"])
    active = client.get("/api/v1/tickets/search?active=true&status=in_progress").json()
    assert active["total"] == 4
    deadlines = client.get("/api/v1/tickets/search?sort=deadline&page_size=100").json()["items"]
    assert [x["resolution_deadline"] for x in deadlines] == sorted(x["resolution_deadline"] for x in deadlines)


def test_saved_views_are_private_limited_and_persistent(env):
    client, Session, ids = env
    ca = signin(client, "a@test.local")
    view = {"name": "Открытые P1", "filters": {
        "q": "pump", "status": "new", "priority": "P1",
        "sort": "deadline", "page_size": 50
    }}
    created = post(client, "/tickets/views", view, ca)
    assert created.status_code == 201, created.text
    own = created.json()
    assert own["filters"]["priority"] == "P1"
    assert client.get("/api/v1/tickets/views").json()[0]["id"] == own["id"]
    assert post(client, "/tickets/views", view, ca).status_code == 409
    assert post(client, "/tickets/views", {"name":"  ", "filters":{}}, ca).status_code == 422
    assert post(client, "/tickets/views", {"name":"x", "filters":{"sort":"sql-injection"}}, ca).status_code == 422
    assert post(client, "/tickets/views", {"name":"x", "filters":{"forbidden":"value"}}, ca).status_code == 422

    signin(client, "b@test.local")
    assert client.get("/api/v1/tickets/views").json() == []
    assert client.delete("/api/v1/tickets/views/" + own["id"],
                         headers={"X-CSRF-Token":client.cookies["fp_csrf"]}).status_code == 404

    signin(client, "a@test.local")
    for i in range(11):
        data={"name":"Фильтр %02d"%i,"filters":{"priority":"P1"}}
        assert post(client, "/tickets/views", data, client.cookies["fp_csrf"]).status_code == 201
    assert len(client.get("/api/v1/tickets/views").json()) == 12
    assert post(client, "/tickets/views", {"name":"overflow","filters":{}},
                client.cookies["fp_csrf"]).status_code == 409
    result = client.delete("/api/v1/tickets/views/" + own["id"],
                           headers={"X-CSRF-Token":client.cookies["fp_csrf"]})
    assert result.status_code == 200 and result.json()["deleted"]
    assert len(client.get("/api/v1/tickets/views").json()) == 11
    with Session() as db:
        assert db.scalar(select(TicketView).where(TicketView.id == own["id"])) is None


def test_operational_diagnostics_are_staff_only(env):
    client, _, _ = env
    signin(client, "a@test.local")
    assert client.get("/api/v1/reports/operations").status_code == 403
    signin(client, "dispatcher@test.local")
    result = client.get("/api/v1/reports/operations")
    assert result.status_code == 200
    body = result.json()
    assert body["unassigned_open_tickets"] == 0
    assert body["open_p1_tickets"] == 0
    assert body["oldest_pending_mock_age_seconds"] is None
    assert body["observed_at"].endswith("+00:00")
