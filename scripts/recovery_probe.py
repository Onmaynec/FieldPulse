"""Snapshot and verify a full PostgreSQL + S3 restore drill."""
import argparse
import hashlib
import json
import os
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from sqlalchemy import func, select

from app.backup import database_report
from app.db import SessionLocal
from app import models as _models
from app.media import BUCKET, bucket_ready, client
from app.models import Attachment, Ticket, User

API = os.getenv("RECOVERY_API_URL", "http://recovery-api:8000")
PASSWORD = os.getenv("DEMO_PASSWORD", "FieldPulseDemo!2026")


def digest_object(s3, key):
    response = s3.get_object(Bucket=BUCKET, Key=key)
    stream = response["Body"]
    hasher = hashlib.sha256()
    size = 0
    try:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
            size += len(chunk)
    finally:
        stream.close()
    return {"sha256": hasher.hexdigest(), "bytes": size}


def storage_objects():
    s3 = client()
    objects = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for item in page.get("Contents", []):
            key = item["Key"]
            if not key.startswith("tickets/") or ".." in key.split("/") or "\\" in key:
                raise AssertionError("Unexpected key in ticket storage")
            objects[key] = digest_object(s3, key)
    return dict(sorted(objects.items()))


def database_snapshot():
    with SessionLocal() as db:
        attachments = db.execute(
            select(Attachment.id, Attachment.ticket_id, Attachment.storage_key,
                   Attachment.thumb_key, Attachment.checksum, Attachment.size,
                   Ticket.org_id)
            .join(Ticket, Ticket.id == Attachment.ticket_id)
            .order_by(Attachment.id)
        ).all()
        client_a = db.scalar(select(User).where(User.email == "client-a@fieldpulse.local"))
        if client_a is None:
            raise AssertionError("Restored demo client A is missing")
        attachment_rows = [
            {"id": row.id, "ticket_id": row.ticket_id, "org_id": row.org_id,
             "storage_key": row.storage_key, "thumb_key": row.thumb_key,
             "checksum": row.checksum, "size": row.size}
            for row in attachments
        ]
        role_counts = {
            role: int(db.scalar(select(func.count(User.id)).where(User.role == role)) or 0)
            for role in ("admin", "dispatcher", "manager", "engineer", "client")
        }
        client_a_org_id = client_a.org_id
    return {
        "database": database_report(),
        "role_counts": role_counts,
        "client_a_org_id": client_a_org_id,
        "attachments": attachment_rows,
        "objects": storage_objects(),
    }


def validate_media_links(snapshot):
    objects = snapshot["objects"]
    for attachment in snapshot["attachments"]:
        original = objects.get(attachment["storage_key"])
        thumbnail = objects.get(attachment["thumb_key"])
        if not original or not thumbnail:
            raise AssertionError("An attachment points to a missing S3 object")
        if original["sha256"] != attachment["checksum"]:
            raise AssertionError("Attachment checksum does not match its original object")
        if original["bytes"] != attachment["size"]:
            raise AssertionError("Attachment byte count does not match its database row")


def write_snapshot(path):
    report = database_snapshot()
    validate_media_links(report)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({
        "database_tables": len(report["database"]["tables"]),
        "attachments": len(report["attachments"]),
        "s3_objects": len(report["objects"]),
        "alembic_revision": report["database"]["alembic_revision"],
        "snapshot": str(target),
    }, ensure_ascii=False))


def request(browser, method, path, payload=None):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if method not in ("GET", "HEAD"):
        csrf = next((cookie.value for cookie in browser["jar"]
                     if cookie.name == "fp_csrf"), "")
        headers["X-CSRF-Token"] = csrf
    req = Request(API + path, data=data, headers=headers, method=method)
    try:
        with browser["opener"].open(req, timeout=20) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def login(email):
    jar = CookieJar()
    browser = {"jar": jar, "opener": build_opener(HTTPCookieProcessor(jar))}
    status, _ = request(browser, "POST", "/api/v1/auth/login",
                        {"email": email, "password": PASSWORD})
    if status != 200:
        raise AssertionError("Restored API login failed for " + email)
    return browser


def verify_roles(snapshot):
    candidates = [item for item in snapshot["attachments"]
                  if item["org_id"] == snapshot["client_a_org_id"]]
    if not candidates:
        raise AssertionError("No restored client A attachment is available for access checks")
    attachment = candidates[0]
    status, _ = request(login("admin@fieldpulse.local"), "GET",
                        "/api/v1/operations/summary")
    if status != 200:
        raise AssertionError("Restored administrator role cannot access operations")
    status, _ = request(login("engineer1@fieldpulse.local"), "GET",
                        "/api/v1/operations/summary")
    if status != 403:
        raise AssertionError("Restored engineer role unexpectedly accessed staff operations")
    status, body = request(login("client-a@fieldpulse.local"), "GET",
                           "/api/v1/attachments/" + attachment["id"])
    if status != 200 or not body:
        raise AssertionError("Restored attachment is not readable by its owning client")
    status, _ = request(login("client-b@fieldpulse.local"), "GET",
                        "/api/v1/attachments/" + attachment["id"])
    if status != 404:
        raise AssertionError("Restored attachment leaked across client organizations")


def verify_snapshot(path):
    expected = json.loads(Path(path).read_text(encoding="utf-8"))
    actual = database_snapshot()
    validate_media_links(actual)
    for field in ("database", "role_counts", "client_a_org_id", "attachments", "objects"):
        if actual[field] != expected[field]:
            raise AssertionError("Restore verification mismatch: " + field)
    if not actual["attachments"] or not actual["objects"]:
        raise AssertionError("Recovery drill must contain real attachments and S3 objects")
    deadline = time.monotonic() + 120
    last_error = None
    while time.monotonic() < deadline:
        try:
            with build_opener().open(API + "/health/ready", timeout=3) as response:
                if response.status == 200:
                    break
        except (OSError, URLError) as error:
            last_error = error
        time.sleep(2)
    else:
        raise AssertionError("Restored API did not become ready: " + str(last_error))
    verify_roles(expected)
    print(json.dumps({
        "result": "verified",
        "database_tables": len(actual["database"]["tables"]),
        "alembic_revision": actual["database"]["alembic_revision"],
        "attachments": len(actual["attachments"]),
        "s3_objects": len(actual["objects"]),
        "role_checks": {
            "admin_operations": "200",
            "engineer_operations": "403",
            "owning_client_attachment": "200",
            "other_client_attachment": "404",
        },
    }, ensure_ascii=False))


def init_bucket():
    last_error = None
    for _ in range(60):
        try:
            bucket_ready(client())
            print("Recovery bucket is ready")
            return
        except Exception as error:
            last_error = error
            time.sleep(2)
    raise RuntimeError("Recovery object storage did not become ready") from last_error


def main():
    parser = argparse.ArgumentParser(description="FieldPulse isolated recovery verification")
    parser.add_argument("action", choices=("snapshot", "verify", "init-bucket"))
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.action == "snapshot":
        if not args.path:
            parser.error("snapshot requires a report path")
        write_snapshot(args.path)
    elif args.action == "verify":
        if not args.path:
            parser.error("verify requires a source report path")
        verify_snapshot(args.path)
    else:
        init_bucket()


if __name__ == "__main__":
    main()
