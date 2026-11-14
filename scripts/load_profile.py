"""Reproducible write-load profile (DEMO ENVIRONMENTS ONLY).

Example: python scripts/load_profile.py --tickets 200 --photos-per-ticket 5
         --workers 8 --allow-writes --output /tmp/fieldpulse-load.json
This creates 200 tickets and 1000 photos; no data cleanup is automatic.
"""
import argparse
import concurrent.futures
import io
import json
import os
import platform
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from pathlib import Path

from PIL import Image

ROOT = os.getenv("LOAD_URL", "http://127.0.0.1:8000")
PASSWORD = os.getenv("DEMO_PASSWORD", "FieldPulseDemo!2026")
local = threading.local()


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 2)


def call(browser, method, path, payload=None, kind="json"):
    headers = {}
    body = None
    if kind == "json" and payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif kind == "multipart":
        boundary = uuid.uuid4().hex
        filename, photo = payload
        body = (("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" +
                 filename + "\"\r\nContent-Type: image/jpeg\r\n\r\n").encode() +
                photo + ("\r\n--" + boundary + "--\r\n").encode())
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    if method not in ("GET", "HEAD"):
        for cookie in browser["jar"]:
            if cookie.name == "fp_csrf":
                headers["X-CSRF-Token"] = cookie.value
    req = Request(ROOT + "/api/v1" + path, method=method, data=body, headers=headers)
    t0 = time.perf_counter()
    status = 0
    try:
        with browser["opener"].open(req, timeout=35) as r:
            status, raw = r.status, r.read()
    except HTTPError as error:
        status, raw = error.code, error.read()
    milliseconds = round((time.perf_counter() - t0) * 1000, 2)
    if status < 200 or status >= 300:
        raise RuntimeError("%s %s: %s %s" % (method, path, status, raw[:300]))
    return json.loads(raw), milliseconds


def get_browser():
    if getattr(local, "browser", None) is None:
        jar = CookieJar()
        browser = {"jar": jar, "opener": build_opener(HTTPCookieProcessor(jar))}
        call(browser, "POST", "/auth/login", {
            "email": "client-a@fieldpulse.local", "password": PASSWORD
        })
        local.browser = browser
    return local.browser


def picture():
    image = Image.new("RGB", (320, 240), (65, 103, 146))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=80)
    return out.getvalue()


def execute(index, site_id, asset_id, photo, photos_per_ticket):
    browser = get_browser()
    metrics = []
    created, ms = call(browser, "POST", "/tickets", {
        "site_id": site_id, "asset_id": asset_id, "priority": "P3",
        "kind": "load_profile",
        "description": "Reproducible benchmark ticket %d" % index,
    })
    metrics.append(("tickets", ms))
    for n in range(photos_per_ticket):
        _, ms = call(browser, "POST", "/tickets/%s/attachments" % created["id"],
                     ("benchmark-%d-%d.jpg" % (index, n), photo), kind="multipart")
        metrics.append(("photos", ms))
    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickets", type=int, default=20)
    parser.add_argument("--photos-per-ticket", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("/tmp/fieldpulse-load.json"))
    args = parser.parse_args()
    if not args.allow_writes:
        parser.error("Mutates the database and S3; pass --allow-writes ONLY on a demo environment")
    if not (1 <= args.tickets <= 10000 and 0 <= args.photos_per_ticket <= 10
            and 1 <= args.workers <= 32):
        parser.error("Limit tickets to 1..10000, photos/ticket 0..10, workers 1..32")
    browser = get_browser()
    sites, _ = call(browser, "GET", "/sites")
    if not sites:
        parser.error("First run demo seed")
    assets, _ = call(browser, "GET", "/assets?site_id=" + sites[0]["id"])
    if not assets:
        parser.error("Demo site has no assets")
    photo = picture()
    timings = {"tickets": [], "photos": []}
    errors = []
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(execute, n, sites[0]["id"], assets[0]["id"],
                               photo, args.photos_per_ticket) for n in range(args.tickets)]
        for future in concurrent.futures.as_completed(futures):
            try:
                results = future.result()
                for kind, ms in results:
                    timings[kind].append(ms)
            except Exception as error:
                errors.append(str(error)[:240])
    elapsed = max(0.001, time.perf_counter() - start)
    report = {
        "profile": {"tickets_requested": args.tickets,
                    "photos_requested": args.tickets * args.photos_per_ticket,
                    "workers": args.workers, "photo_bytes": len(photo)},
        "environment": {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "os": platform.platform(), "python": platform.python_version(),
                        "cpu_count": os.cpu_count()},
        "elapsed_seconds": round(elapsed, 2),
        "operations": {
            kind: {
                "completed": len(values), "p50_ms": percentile(values, 0.5),
                "p95_ms": percentile(values, 0.95),
                "max_ms": max(values) if values else None,
                "throughput_per_second": round(len(values)/elapsed, 2)
            }
            for kind, values in timings.items()
        },
        "failed_tasks": len(errors), "errors": errors[:25],
        "limitations": "Synthetic local image, single site, no real network outages. "
                       "Results apply only to this test environment.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
