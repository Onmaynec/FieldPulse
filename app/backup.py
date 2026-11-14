"""PostgreSQL + S3 backup archive with SHA-256 manifest and isolated restore.

Create only during a maintenance window with application writers stopped:
  python -m app.backup create /backups/fieldpulse.zip --maintenance-confirmed
Verify without changing services:
  python -m app.backup verify /backups/fieldpulse.zip
Restore ONLY into a fresh, isolated, empty database and bucket:
  python -m app.backup restore /backups/fieldpulse.zip --confirm-empty

PostgreSQL pg_dump is transactionally consistent. S3 enumeration is not part
of that transaction, so stopping API/worker/monitor is required for a
cross-store point-in-time copy. PostgreSQL and S3 restore are not atomic.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url

from .db import Base, DATABASE_URL, SessionLocal, engine
from . import models as _models  # Register every model in Base.metadata.
from .media import BUCKET, bucket_ready, client
from .models import Attachment, Organization, Ticket, User

FORMAT = "fieldpulse-backup-1"
MAX_MEMBER = 1024 * 1024 * 1024  # Refuse a single archive member over 1 GiB.


def checksum_file(path):
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
            size += len(chunk)
    return {"sha256": hasher.hexdigest(), "bytes": size}


def checksum_stream(stream):
    hasher = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        hasher.update(chunk)
        size += len(chunk)
    return {"sha256": hasher.hexdigest(), "bytes": size}


def checksum_archive_entry(archive, name):
    with archive.open(name) as stream:
        return checksum_stream(stream)


def checksum_s3_object(s3, key):
    response = s3.get_object(Bucket=BUCKET, Key=key)
    stream = response["Body"]
    try:
        return checksum_stream(stream)
    finally:
        stream.close()


def pg_command(program, filename):
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("Backup/restore requires PostgreSQL")
    command = [
        program, "--host", url.host or "localhost",
        "--port", str(url.port or 5432),
        "--username", url.username or "fieldpulse",
        "--dbname", url.database,
    ]
    environment = {**os.environ, "PGPASSWORD": url.password or ""}
    if program == "pg_dump":
        command += ["--format=custom", "--no-owner", "--no-acl",
                    "--file", str(filename)]
    elif program == "pg_restore":
        command += ["--clean", "--if-exists", "--no-owner", "--no-acl",
                    "--exit-on-error", "--single-transaction", str(filename)]
    else:
        raise ValueError("Unsupported PostgreSQL backup program")
    return command, environment


def run_pg(program, filename):
    command, environment = pg_command(program, filename)
    try:
        subprocess.run(command, env=environment, check=True,
                       capture_output=True, timeout=900)
    except subprocess.CalledProcessError as error:
        # Do not forward command output: it may contain database names or paths.
        raise RuntimeError("%s failed with exit code %s" %
                           (program, error.returncode)) from None


def database_report():
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    missing = expected - present
    if missing:
        raise RuntimeError("Restored schema is incomplete: " + ", ".join(sorted(missing)))
    with SessionLocal() as db:
        counts = {
            name: int(db.scalar(select(func.count()).select_from(Base.metadata.tables[name])) or 0)
            for name in sorted(expected)
        }
        revision = None
        if "alembic_version" in present:
            revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return {"tables": counts, "alembic_revision": revision}


def store_archive(out, *, maintenance_confirmed=False):
    if not maintenance_confirmed:
        raise RuntimeError(
            "Backup requires --maintenance-confirmed: stop API, worker and monitor first"
        )
    target = Path(out).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError("Refusing to overwrite existing backup: " + str(target))
    staging = target.with_name(target.name + ".partial")
    if staging.exists():
        raise FileExistsError(staging)

    s3 = client()
    s3.head_bucket(Bucket=BUCKET)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            pg = Path(tmp) / "database.dump"
            run_pg("pg_dump", pg)
            members = {}
            with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as archive:
                archive.write(pg, "postgres/database.dump")
                members["postgres/database.dump"] = checksum_file(pg)
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=BUCKET):
                    for item in page.get("Contents", []):
                        key = item["Key"]
                        if (not key.startswith("tickets/")
                                or ".." in key.split("/")
                                or "\\" in key):
                            raise ValueError("Unexpected object key in storage")
                        name = "s3/" + key
                        if name in members:
                            raise ValueError("Duplicate object key in storage")
                        response = s3.get_object(Bucket=BUCKET, Key=key)
                        stream = response["Body"]
                        try:
                            with archive.open(name, "w") as destination:
                                digest = hashlib.sha256()
                                size = 0
                                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                    destination.write(chunk)
                                    digest.update(chunk)
                                    size += len(chunk)
                            members[name] = {"sha256": digest.hexdigest(), "bytes": size}
                        finally:
                            stream.close()
                manifest = {
                    "format": FORMAT,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "bucket": BUCKET,
                    "snapshot_policy": (
                        "PostgreSQL pg_dump snapshot; S3 copied sequentially. "
                        "Application writers must remain stopped for the full operation."
                    ),
                    "members": members,
                }
                archive.writestr("manifest.json",
                                 json.dumps(manifest, indent=2, ensure_ascii=False))
        staging.replace(target)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return {"path": str(target), "members": len(members),
            "s3_objects": len(members) - 1, "bytes": target.stat().st_size}


def verify_archive(path):
    with zipfile.ZipFile(path, "r") as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError("Archive contains duplicate filenames")
        if "manifest.json" not in names:
            raise ValueError("Backup manifest is missing")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise ValueError("Unsupported backup format")
        members = manifest.get("members")
        if (not isinstance(members, dict)
                or "postgres/database.dump" not in members):
            raise ValueError("Invalid backup manifest")
        if set(names) != set(members) | {"manifest.json"}:
            raise ValueError("Archive and manifest list different entries")
        for entry in entries:
            name = entry.filename
            if name == "manifest.json":
                continue
            if (entry.file_size > MAX_MEMBER or name.startswith("/")
                    or "\\" in name or ".." in name.split("/")):
                raise ValueError("Unsafe backup entry")
            if name != "postgres/database.dump" and not name.startswith("s3/tickets/"):
                raise ValueError("Unexpected backup entry")
            expected = members[name]
            if (not isinstance(expected, dict)
                    or not isinstance(expected.get("sha256"), str)
                    or len(expected["sha256"]) != 64
                    or type(expected.get("bytes")) is not int
                    or expected["bytes"] < 0):
                raise ValueError("Invalid checksum manifest entry")
            observed = checksum_archive_entry(archive, name)
            if expected != observed:
                raise ValueError("Checksum mismatch: " + name)
        return {"valid": True, "members": len(members),
                "s3_objects": len(members) - 1, "manifest": manifest}


def ensure_empty_target():
    tables = set(inspect(engine).get_table_names())
    application_tables = tables - {"alembic_version"}
    if application_tables:
        raise RuntimeError(
            "Restore requires a fresh database without application tables; found: "
            + ", ".join(sorted(application_tables))
        )
    if "alembic_version" in tables:
        with engine.connect() as connection:
            if connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar():
                raise RuntimeError("Restore target already has an Alembic revision")


def restore_archive(path):
    report = verify_archive(path)
    if report["manifest"]["bucket"] != BUCKET:
        raise ValueError("Backup bucket does not match target S3_BUCKET")

    # Validate both destinations before the first write.
    ensure_empty_target()
    s3 = client()
    bucket_ready(s3)
    if s3.list_objects_v2(Bucket=BUCKET, MaxKeys=1).get("KeyCount", 0):
        raise RuntimeError("Restore requires an empty object-storage bucket")

    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path) as archive:
        dump = Path(tmp) / "database.dump"
        with archive.open("postgres/database.dump") as source, dump.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        run_pg("pg_restore", dump)

        # Verify schema and row readability before starting the API.
        database = database_report()
        restored = 0
        for name, expected in report["manifest"]["members"].items():
            if not name.startswith("s3/"):
                continue
            key = name[3:]
            with archive.open(name) as source:
                s3.upload_fileobj(source, BUCKET, key,
                                  ExtraArgs={"ContentType": "image/jpeg"})
            observed = checksum_s3_object(s3, key)
            if observed != expected:
                raise RuntimeError(
                    "Restored object checksum mismatch; discard this isolated target"
                )
            restored += 1

    return {"restored_s3_objects": restored,
            "database": database,
            "note": (
                "PostgreSQL schema and S3 object hashes verified. "
                "Database and object storage are not an atomic transaction."
            )}


def main():
    parser = argparse.ArgumentParser(description="FieldPulse PostgreSQL/S3 backup and recovery")
    parser.add_argument("action", choices=("create", "verify", "restore"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("--maintenance-confirmed", action="store_true",
                        help="Confirm API, worker and monitor are stopped")
    parser.add_argument("--confirm-empty", action="store_true",
                        help="Confirm this is a new isolated empty database and bucket")
    args = parser.parse_args()
    if args.action == "create":
        result = store_archive(args.archive,
                               maintenance_confirmed=args.maintenance_confirmed)
    elif args.action == "verify":
        report = verify_archive(args.archive)
        result = {key: value for key, value in report.items() if key != "manifest"}
    else:
        if not args.confirm_empty:
            parser.error("restore requires --confirm-empty")
        result = restore_archive(args.archive)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
