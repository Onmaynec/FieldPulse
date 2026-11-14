"""FieldPulse disaster recovery: PostgreSQL + S3 archive with SHA-256 manifest.

Usage inside API container:
  python -m app.backup create /tmp/fieldpulse-backup.zip
  python -m app.backup verify /tmp/fieldpulse-backup.zip
  python -m app.backup restore /tmp/fieldpulse-backup.zip --confirm-empty

Restore is intended ONLY for a new empty isolated instance with API and worker
stopped. PostgreSQL restore and S3 upload cannot be an atomic transaction.
"""
import argparse
import hashlib
import json
import shutil
import os
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from .db import DATABASE_URL, SessionLocal
from .media import BUCKET, client
from .models import Organization, Ticket, User

FORMAT = "fieldpulse-backup-1"
MAX_MEMBER = 1024 * 1024 * 1024  # Abort suspicious, oversized archives.


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def checksum_file(path):
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
            size += len(chunk)
    return {"sha256": hasher.hexdigest(), "bytes": size}


def checksum_archive_entry(archive, name):
    hasher = hashlib.sha256()
    size = 0
    with archive.open(name) as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            size += len(chunk)
            hasher.update(chunk)
    return {"sha256": hasher.hexdigest(), "bytes": size}


def pg_command(program, filename):
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("Backup/restore requires PostgreSQL")
    cmd = [program, "--host", url.host or "localhost",
           "--port", str(url.port or 5432),
           "--username", url.username or "fieldpulse", "--dbname", url.database]
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    if program == "pg_dump":
        cmd += ["--format=custom", "--no-owner", "--no-acl", "--file", str(filename)]
    else:
        cmd += ["--clean", "--if-exists", "--no-owner", "--no-acl",
                "--single-transaction", str(filename)]
    return cmd, env


def run_pg(program, filename):
    cmd, env = pg_command(program, filename)
    subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=300)


def store_archive(out):
    target = Path(out).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError("Refusing to overwrite existing backup: " + str(target))
    s3 = client()
    s3.head_bucket(Bucket=BUCKET)
    with tempfile.TemporaryDirectory() as tmp:
        pg = Path(tmp) / "database.dump"
        run_pg("pg_dump", pg)
        names = {}
        staging = target.with_name(target.name + ".partial")
        if staging.exists():
            raise FileExistsError(staging)
        try:
            with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as z:
                z.write(pg, "postgres/database.dump")
                names["postgres/database.dump"] = checksum_file(pg)
                paginator = s3.get_paginator("list_objects_v2")
                for part in paginator.paginate(Bucket=BUCKET):
                    for item in part.get("Contents", []):
                        key = item["Key"]
                        if not key.startswith("tickets/") or ".." in key.split("/"):
                            raise ValueError("Unexpected object key: " + key)
                        path = "s3/" + key
                        if path in names:
                            raise ValueError("Duplicate key: " + key)
                        stream = s3.get_object(Bucket=BUCKET, Key=key)["Body"]
                        hasher = hashlib.sha256()
                        size = 0
                        try:
                            with z.open(path, "w") as dest:
                                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                    size += len(chunk)
                                    hasher.update(chunk)
                                    dest.write(chunk)
                        finally:
                            stream.close()
                        names[path] = {"sha256": hasher.hexdigest(), "bytes": size}
                manifest = {
                    "format": FORMAT,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "bucket": BUCKET,
                    "members": names,
                }
                z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            staging.replace(target)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
    return {"path": str(target), "members": len(names),
            "s3_objects": len(names) - 1}


def verify_archive(path):
    with zipfile.ZipFile(path, "r") as archive:
        entries = archive.infolist()
        entry_names = [item.filename for item in entries]
        if len(entry_names) != len(set(entry_names)):
            raise ValueError("Archive contains repeated filenames")
        if "manifest.json" not in entry_names:
            raise ValueError("Backup manifest is missing")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError("Unsupported backup format")
        members = manifest.get("members")
        if not isinstance(members, dict) or "postgres/database.dump" not in members:
            raise ValueError("Invalid backup manifest")
        if set(entry_names) != set(members) | {"manifest.json"}:
            raise ValueError("Archive and manifest list different entries")
        for item in entries:
            if item.filename == "manifest.json":
                continue
            if (item.file_size > MAX_MEMBER or item.filename.startswith("/")
                or "\\" in item.filename or ".." in item.filename.split("/")):
                raise ValueError("Unsafe backup entry")
            if item.filename != "postgres/database.dump" and not item.filename.startswith("s3/tickets/"):
                raise ValueError("Unexpected backup entry")
            expected = members[item.filename]
            observed = checksum_archive_entry(archive, item.filename)
            if not isinstance(expected, dict) or expected != observed:
                raise ValueError("Checksum mismatch: " + item.filename)
        return {"valid": True, "members": len(members),
                "s3_objects": len(members) - 1, "manifest": manifest}


def restore_archive(path):
    report = verify_archive(path)  # Validate before touching PostgreSQL/S3.
    if report["manifest"]["bucket"] != BUCKET:
        raise ValueError("Backup bucket does not match target S3_BUCKET")
    with SessionLocal() as db:
        if (db.scalar(select(func.count(Organization.id))) or 0) > 0 or (
            db.scalar(select(func.count(User.id))) or 0) > 0 or (
            db.scalar(select(func.count(Ticket.id))) or 0) > 0:
            raise RuntimeError("Restore only to an EMPTY database")
    s3 = client()
    objects = s3.list_objects_v2(Bucket=BUCKET, MaxKeys=1)
    if objects.get("KeyCount", 0):
        raise RuntimeError("Restore only to an EMPTY object-storage bucket")
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path) as archive:
        dest = Path(tmp) / "database.dump"
        with archive.open("postgres/database.dump") as src, dest.open("wb") as target:
            shutil.copyfileobj(src, target, length=1024 * 1024)
        run_pg("pg_restore", dest)
        restored = 0
        for filename in report["manifest"]["members"]:
            if filename.startswith("s3/"):
                key = filename[3:]
                with archive.open(filename) as obj:
                    s3.upload_fileobj(obj, BUCKET, key,
                                      ExtraArgs={"ContentType": "image/jpeg"})
                restored += 1
    return {"restored_s3_objects": restored,
            "note": "Restore is not atomic across PostgreSQL and S3; verify immediately."}


def main():
    parser = argparse.ArgumentParser(description="FieldPulse backup and verification")
    parser.add_argument("action", choices=("create", "verify", "restore"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("--confirm-empty", action="store_true",
                        help="Acknowledge recovery on a new isolated, empty instance")
    args = parser.parse_args()
    if args.action == "create":
        print(json.dumps(store_archive(args.archive), ensure_ascii=False))
    elif args.action == "verify":
        report = verify_archive(args.archive)
        print(json.dumps({k: v for k, v in report.items() if k != "manifest"}, ensure_ascii=False))
    else:
        if not args.confirm_empty:
            parser.error("restore requires --confirm-empty and a stopped API/worker")
        print(json.dumps(restore_archive(args.archive), ensure_ascii=False))


if __name__ == "__main__":
    main()
