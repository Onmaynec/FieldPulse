"""End-to-end API smoke on actual PostgreSQL + S3 service.

Run with the full Compose stack and seed. The script uses only stdlib and PIL,
so it runs directly inside the API container without test-only dependencies.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import uuid
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from PIL import Image

ROOT = os.getenv("SMOKE_URL", "http://127.0.0.1:8000")
PASSWORD = os.getenv("DEMO_PASSWORD", "FieldPulseDemo!2026")


class Browser:
    def __init__(self):
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))

    def request(self, method, path, payload=None, headers=None, expected=200):
        headers = dict(headers or {})
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode()
        if method not in ("GET", "HEAD"):
            for cookie in self.jar:
                if cookie.name == "fp_csrf":
                    headers["X-CSRF-Token"] = cookie.value
        req = Request(ROOT + "/api/v1" + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=25) as reply:
                status = reply.status
                raw = reply.read()
        except HTTPError as exc:
            status = exc.code
            raw = exc.read()
        data = json.loads(raw)
        assert status == expected, (method, path, status, expected, data)
        return data

    def login(self, email):
        return self.request("POST", "/auth/login",
                            {"email": email, "password": PASSWORD})


def jpeg_data():
    im = Image.new("RGB", (28, 28), (220, 132, 75))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=84)
    return base64.b64encode(buf.getvalue()).decode()


def main():
    client_a = Browser()
    client_b = Browser()
    dispatcher = Browser()
    client_a.login("client-a@fieldpulse.local")
    client_b.login("client-b@fieldpulse.local")
    dispatcher.login("dispatcher@fieldpulse.local")
    sites = client_a.request("GET", "/sites")
    assert sites
    site = sites[0]
    equipment = client_a.request("GET", "/assets?site_id=" + site["id"])
    assert equipment
    asset = equipment[0]
    payload = {
        "site_id": site["id"], "asset_id": asset["id"],
        "priority": "P1", "kind": "repair",
        "description": "Compose smoke: field electrician repairs electrical cabinet"
    }
    key = str(uuid.uuid4())
    ticket = client_a.request("POST", "/tickets", payload, {"Idempotency-Key": key}, expected=201)
    copy = client_a.request("POST", "/tickets", payload, {"Idempotency-Key": key}, expected=201)
    assert ticket["id"] == copy["id"]
    client_b.request("GET", "/tickets/" + ticket["id"], expected=404)
    client_b.request("GET", "/tickets/" + ticket["id"] + "/timeline", expected=404)
    client_a.request("POST", "/tickets/" + ticket["id"] + "/assign",
                     {"version": ticket["version"], "engineer_id": "fake"}, expected=403)
    options = dispatcher.request("GET", "/engineers/suggestions?ticket_id=" + ticket["id"])
    qualified = [e for e in options if e["eligible"]]
    assert qualified, "No available engineer: check seed active workload and shifts"
    choice = qualified[0]
    assigned = dispatcher.request("POST", "/tickets/" + ticket["id"] + "/assign", {
        "version": ticket["version"], "engineer_id": choice["id"]
    })
    engineer = Browser()
    engineer_num = int(choice["name"].split()[-1])
    engineer.login("engineer%d@fieldpulse.local" % engineer_num)
    accepted = engineer.request("POST", "/tickets/" + ticket["id"] + "/accept",
                                {"version": assigned["version"]})
    started = engineer.request("POST", "/tickets/" + ticket["id"] + "/start",
                               {"version": accepted["version"]})
    required = engineer.request("GET", "/tickets/" + ticket["id"] + "/checklist")["required"]
    action = {
        "uuid": str(uuid.uuid4()), "ticket_id": ticket["id"],
        "base_version": started["version"],
        "client_at": "2026-11-13T17:50:00+03:00",
        "summary": "Smoke: inspected and replaced module",
        "checklist": {key: True for key in required},
        "materials": [{"name": "Replacement component", "quantity": 1}],
        "photos": [
            {"name": "before.jpg", "data": jpeg_data()},
            {"name": "after.jpg", "data": jpeg_data()}
        ]
    }
    # Both sessions send the same UUID at once; row locks must serialize the replay.
    another_session = Browser()
    another_session.login("engineer%d@fieldpulse.local" % engineer_num)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(engineer.request, "POST", "/mobile/sync", {"actions": [action]})
        b = pool.submit(another_session.request, "POST", "/mobile/sync", {"actions": [action]})
        statuses = sorted([a.result()["results"][0]["status"], b.result()["results"][0]["status"]])
    assert statuses == ["applied", "duplicate"], statuses
    duplicate = engineer.request("POST", "/mobile/sync", {"actions": [action]})
    assert duplicate["results"][0]["status"] == "duplicate"
    photos = engineer.request("GET", "/tickets/" + ticket["id"] + "/attachments")
    assert len(photos) == 2, photos
    latest = engineer.request("GET", "/tickets/" + ticket["id"])
    act = engineer.request("POST", "/tickets/" + ticket["id"] + "/submit",
                           {"version": latest["version"]})
    assert act["status"] == "pending"
    client_b.request("GET", "/acts/" + act["id"], expected=404)
    rejected = client_a.request("POST", "/acts/" + act["id"] + "/decision",
                                {"version": act["version"], "decision": "rejected",
                                 "reason": "Повторить замеры"})
    assert rejected["status"] == "rejected"
    revisions = client_a.request("GET", "/acts")
    assert any(x["ticket_id"] == ticket["id"] and x["version"] == 2 and x["status"] == "draft"
               for x in revisions)
    print("FieldPulse smoke: client tenant isolation, idempotent creation, matching, "
          "acceptance, 2 photos via S3, concurrent duplicate sync, act revisions — OK")


if __name__ == "__main__":
    main()
