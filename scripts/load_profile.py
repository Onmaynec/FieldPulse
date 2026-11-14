"""Controlled write-load profile. Run only on an isolated demo/staging system.

Short burst:
  python scripts/load_profile.py --tickets 20 --photos-per-ticket 2 --workers 4 --allow-writes
Daily profile:
  python scripts/load_profile.py --tickets 200 --photos-per-ticket 5 --workers 4
    --duration-hours 24 --photo-width 1600 --photo-height 1200 --photo-quality 86
    --allow-writes --output /backups/fieldpulse-24h.json

Creates real rows and S3 objects. It never deletes data automatically.
"""
import argparse
import concurrent.futures
import io
import json
import os
import platform
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from pathlib import Path

from PIL import Image
from sqlalchemy import text

from app.db import engine

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
    if method == "POST" and path == "/tickets":
        headers["Idempotency-Key"] = uuid.uuid4().hex
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
    started = time.perf_counter()
    try:
        with browser["opener"].open(req, timeout=60) as response:
            status, raw = response.status, response.read()
    except HTTPError as error:
        status, raw = error.code, error.read()
    milliseconds = round((time.perf_counter() - started) * 1000, 2)
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


def picture(width, height, quality):
    # Noise is deliberately less compressible than a flat-color fixture and
    # gives the storage path a more representative payload size.
    image = Image.effect_noise((width, height), 90).convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality, optimize=True)
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
    for number in range(photos_per_ticket):
        _, ms = call(browser, "POST", "/tickets/%s/attachments" % created["id"],
                     ("benchmark-%d-%d.jpg" % (index, number), photo), kind="multipart")
        metrics.append(("photos", ms))
    return metrics


def memory_total_bytes():
    try:
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def database_profile():
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version()")).scalar()
        settings = {}
        for name in ("max_connections", "shared_buffers", "work_mem", "effective_cache_size"):
            settings[name] = connection.execute(text("SHOW " + name)).scalar()
    return {"version": version, "settings": settings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickets", type=int, default=20)
    parser.add_argument("--photos-per-ticket", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--duration-hours", type=float, default=0,
                        help="Spread ticket creation evenly over this duration; 0 means burst")
    parser.add_argument("--photo-width", type=int, default=320)
    parser.add_argument("--photo-height", type=int, default=240)
    parser.add_argument("--photo-quality", type=int, default=80)
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("/tmp/fieldpulse-load.json"))
    args = parser.parse_args()
    if not args.allow_writes:
        parser.error("Mutates PostgreSQL and S3; pass --allow-writes ONLY on isolated demo/staging")
    if not (1 <= args.tickets <= 10000 and 0 <= args.photos_per_ticket <= 10
            and 1 <= args.workers <= 32):
        parser.error("Limit tickets to 1..10000, photos/ticket 0..10, workers 1..32")
    if not (0 <= args.duration_hours <= 168):
        parser.error("--duration-hours must be between 0 and 168")
    if not (64 <= args.photo_width <= 5000 and 64 <= args.photo_height <= 5000
            and args.photo_width * args.photo_height <= 25000000
            and 40 <= args.photo_quality <= 95):
        parser.error("Photo dimensions/quality exceed safe image processing limits")

    browser = get_browser()
    sites, _ = call(browser, "GET", "/sites")
    if not sites:
        parser.error("First run demo seed")
    assets, _ = call(browser, "GET", "/assets?site_id=" + sites[0]["id"])
    if not assets:
        parser.error("Demo site has no assets")
    photo = picture(args.photo_width, args.photo_height, args.photo_quality)
    if len(photo) > 10 * 1024 * 1024:
        parser.error("Generated JPEG exceeds the application's 10 MiB upload limit")

    timings = {"tickets": [], "photos": []}
    errors = []
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = []
        duration_seconds = args.duration_hours * 3600
        for index in range(args.tickets):
            if duration_seconds:
                due = started + duration_seconds * index / args.tickets
                wait = due - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
            futures.append(pool.submit(
                execute, index, sites[0]["id"], assets[0]["id"],
                photo, args.photos_per_ticket
            ))
        for future in concurrent.futures.as_completed(futures):
            try:
                for kind, milliseconds in future.result():
                    timings[kind].append(milliseconds)
            except Exception as error:
                errors.append(str(error)[:240])
    elapsed = max(0.001, time.perf_counter() - started)
    disk = shutil.disk_usage("/")
    report = {
        "profile": {
            "tickets_requested": args.tickets,
            "photos_requested": args.tickets * args.photos_per_ticket,
            "workers": args.workers,
            "duration_hours_requested": args.duration_hours,
            "photo_width": args.photo_width,
            "photo_height": args.photo_height,
            "photo_quality": args.photo_quality,
            "photo_bytes": len(photo),
        },
        "environment": {
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "os": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "memory_total_bytes": memory_total_bytes(),
            "filesystem_total_bytes": disk.total,
            "filesystem_free_bytes": disk.free,
            "database": database_profile(),
        },
        "elapsed_seconds": round(elapsed, 2),
        "operations": {
            kind: {
                "completed": len(values),
                "p50_ms": percentile(values, 0.5),
                "p95_ms": percentile(values, 0.95),
                "max_ms": max(values) if values else None,
                "throughput_per_second": round(len(values) / elapsed, 4),
            }
            for kind, values in timings.items()
        },
        "failed_tasks": len(errors),
        "errors": errors[:25],
        "limitations": (
            "Synthetic generated JPEGs, one demo tenant/site, no real network loss or "
            "device variability. A 24-hour result is valid only if the full requested "
            "duration elapsed and all requested tickets/photos completed."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False))
    if errors or len(timings["tickets"]) != args.tickets or len(timings["photos"]) != args.tickets * args.photos_per_ticket:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
