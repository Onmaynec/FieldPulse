"""Report stale S3 objects unreferenced by Attachment rows; deletion opt-in only."""
import argparse
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .db import SessionLocal
from .media import BUCKET, client
from .models import Attachment

KEY_PATTERN = re.compile(r"^tickets/[0-9a-f]{32}/[0-9a-f]{32}(?:-thumb)?\.jpg$")


def candidates(objects, referenced, cutoff):
    """Pure, deterministic scanner: never consider in-flight/recent uploads."""
    return [
        x["Key"] for x in objects
        if x["Key"] not in referenced and KEY_PATTERN.fullmatch(x["Key"])
        and x["LastModified"].astimezone(timezone.utc) < cutoff
    ]


def scan(grace_hours=24, delete=False, confirm=False, limit=100):
    if grace_hours < 24:
        raise ValueError("At least 24 hours of safety grace are required")
    if limit < 1 or limit > 1000:
        raise ValueError("Limit must be 1..1000")
    if delete and not confirm:
        raise ValueError("Deletion requires --confirm")
    with SessionLocal() as db:
        references = set()
        for key, thumb in db.execute(select(Attachment.storage_key, Attachment.thumb_key)):
            references.add(key)
            references.add(thumb)
    storage = client()
    listed = []
    for page in storage.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="tickets/"):
        listed.extend(page.get("Contents", []))
    stale = candidates(listed, references, datetime.now(timezone.utc) - timedelta(hours=grace_hours))
    selected = stale[:limit]
    if delete:
        # Repeat the DB check immediately before any destructive operation.
        with SessionLocal() as db:
            current = set()
            for key, thumb in db.execute(select(Attachment.storage_key, Attachment.thumb_key)):
                current.add(key)
                current.add(thumb)
        selected = [key for key in selected if key not in current]
        for key in selected:
            storage.delete_object(Bucket=BUCKET, Key=key)
    return {"candidates": len(stale), "shown_or_deleted": selected,
            "deleted": delete, "grace_hours": grace_hours,
            "truncated": len(stale) > limit}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--grace-hours", type=int, default=24)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--delete", action="store_true")
    p.add_argument("--confirm", action="store_true")
    args = p.parse_args()
    print(json.dumps(scan(args.grace_hours, args.delete, args.confirm, args.limit),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
