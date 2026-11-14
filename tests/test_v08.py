"""v0.8 recovery safety and object-store compensation."""
import hashlib
import io
import json
import zipfile

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from PIL import Image

from app import backup, media


def make_jpeg():
    image = Image.new("RGB", (64, 48), (70, 110, 155))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def make_archive(path, payload=b"postgres-dump"):
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "format": backup.FORMAT,
        "created_at": "2026-11-15T00:00:00+00:00",
        "bucket": "fieldpulse",
        "snapshot_policy": "test",
        "members": {"postgres/database.dump": {"sha256": digest, "bytes": len(payload)}},
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("postgres/database.dump", payload)
        archive.writestr("manifest.json", json.dumps(manifest))


def test_backup_creation_requires_explicit_maintenance_window(tmp_path):
    with pytest.raises(RuntimeError, match="maintenance-confirmed"):
        backup.store_archive(tmp_path / "backup.zip")


def test_backup_archive_verifies_and_rejects_modified_dump(tmp_path):
    good = tmp_path / "good.zip"
    make_archive(good)
    report = backup.verify_archive(good)
    assert report["valid"] is True
    assert report["members"] == 1
    assert report["s3_objects"] == 0

    bad = tmp_path / "bad.zip"
    manifest = {
        "format": backup.FORMAT,
        "created_at": "2026-11-15T00:00:00+00:00",
        "bucket": "fieldpulse",
        "members": {"postgres/database.dump": {
            "sha256": hashlib.sha256(b"postgres-dump").hexdigest(),
            "bytes": len(b"postgres-dump"),
        }},
    }
    with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("postgres/database.dump", b"changed-dump")
        archive.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="Checksum mismatch"):
        backup.verify_archive(bad)


def test_backup_verifier_rejects_path_traversal(tmp_path):
    path = tmp_path / "unsafe.zip"
    payload = b"unsafe"
    manifest = {
        "format": backup.FORMAT,
        "bucket": "fieldpulse",
        "members": {
            "postgres/database.dump": {
                "sha256": hashlib.sha256(b"pg").hexdigest(), "bytes": 2
            },
            "s3/tickets/../outside.jpg": {
                "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)
            },
        },
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("postgres/database.dump", b"pg")
        archive.writestr("s3/tickets/../outside.jpg", payload)
        archive.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsafe backup entry"):
        backup.verify_archive(path)


def test_pg_restore_is_single_transaction_and_stops_on_first_error(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "DATABASE_URL",
                        "postgresql://restore-user:restore-pass@restore-db:5432/fieldpulse")
    command, environment = backup.pg_command("pg_restore", tmp_path / "db.dump")
    assert "--single-transaction" in command
    assert "--exit-on-error" in command
    assert "--no-owner" in command and "--no-acl" in command
    assert environment["PGPASSWORD"] == "restore-pass"
    assert "restore-pass" not in " ".join(command)


def test_partial_image_upload_removes_the_first_object(monkeypatch):
    class FakeStorage:
        def __init__(self):
            self.puts = []
            self.deleted = []

        def put_object(self, **kwargs):
            self.puts.append(kwargs["Key"])
            if len(self.puts) == 2:
                raise ClientError(
                    {"Error": {"Code": "InternalError", "Message": "simulated interruption"},
                     "ResponseMetadata": {"HTTPStatusCode": 500}},
                    "PutObject",
                )

        def delete_object(self, **kwargs):
            self.deleted.append(kwargs["Key"])

    fake = FakeStorage()
    monkeypatch.setattr(media, "client", lambda: fake)
    monkeypatch.setattr(media, "bucket_ready", lambda storage: None)
    with pytest.raises(HTTPException) as error:
        media.save_image(make_jpeg(), "ticket-recovery-test")
    assert error.value.status_code == 503
    assert len(fake.puts) == 2
    assert fake.deleted == [fake.puts[0]]


def test_delete_image_attempts_both_original_and_thumbnail(monkeypatch):
    class FakeStorage:
        def __init__(self):
            self.deleted = []

        def delete_object(self, **kwargs):
            self.deleted.append(kwargs["Key"])

    fake = FakeStorage()
    monkeypatch.setattr(media, "client", lambda: fake)
    assert media.delete_image({
        "storage_key": "tickets/a/original.jpg",
        "thumb_key": "tickets/a/original-thumb.jpg",
    })
    assert fake.deleted == [
        "tickets/a/original.jpg", "tickets/a/original-thumb.jpg"
    ]
