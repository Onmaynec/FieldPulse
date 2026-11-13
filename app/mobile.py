"""Offline action replay: each UUID is processed once, conflicts are explicit and preserved."""
import base64
import binascii
import io
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api import emit, fmt, serialize_ticket, template, ticket_for
from .db import get_db
from .models import Attachment, Ticket, User, Worklog, now
from .security import current_user, roles
from .media import save_image, read_image

router = APIRouter(prefix="/api/v1")

class Photo(BaseModel):
    name: str = Field(max_length=120)
    data: str = Field(max_length=15000000)

class Action(BaseModel):
    uuid: uuid.UUID
    ticket_id: str
    base_version: int = Field(ge=1)
    client_at: datetime
    summary: str = Field(default="", max_length=2000)
    checklist: dict[str, bool] = Field(default_factory=dict)
    materials: list[dict] = Field(default_factory=list, max_length=30)
    photos: list[Photo] = Field(default_factory=list, max_length=5)

class Batch(BaseModel):
    actions: list[Action] = Field(max_length=30)

@router.post("/mobile/sync")
def sync(batch: Batch, user: User = Depends(roles("engineer")),
         db: Session = Depends(get_db)):
    results = []
    for action in batch.actions:
        key = str(action.uuid)
        previous = db.scalar(select(Worklog).where(
            Worklog.engineer_id == user.id, Worklog.action_uuid == key))
        if previous:
            results.append({"uuid": key, "status": "duplicate", "worklog_id": previous.id,
                            "server_at": fmt(previous.server_at)})
            continue
        t = db.scalar(select(Ticket).where(Ticket.id == action.ticket_id).with_for_update())
        if not t or t.assignee_id != user.id:
            results.append({"uuid": key, "status": "conflict", "reason": "NOT_ASSIGNED"})
            continue
        if t.version != action.base_version or t.status not in ("accepted", "in_progress", "returned"):
            results.append({"uuid": key, "status": "conflict", "reason": "VERSION_OR_STATUS",
                            "server_version": t.version, "server_status": t.status})
            continue
        required = set(template(db, t))
        if not set(action.checklist).issubset(required):
            results.append({"uuid": key, "status": "invalid", "reason": "UNKNOWN_CHECKLIST_ITEM"})
            continue
        if any(not isinstance(m.get("name"), str) or not isinstance(m.get("quantity"), (int, float))
               or m["quantity"] < 0 for m in action.materials):
            results.append({"uuid": key, "status": "invalid", "reason": "INVALID_MATERIAL"})
            continue
        try:
            decoded = [(p.name, base64.b64decode(p.data, validate=True)) for p in action.photos]
            if any(len(raw) > 10 * 1024 * 1024 for _, raw in decoded):
                raise ValueError("FILE_TOO_LARGE")
        except (binascii.Error, ValueError) as exc:
            results.append({"uuid": key, "status": "invalid", "reason": str(exc)})
            continue
        try:
            w = Worklog(ticket_id=t.id, engineer_id=user.id, action_uuid=key,
                        summary=action.summary, checklist=action.checklist,
                        materials=action.materials, client_at=action.client_at, server_at=now())
            db.add(w)
            db.flush()
            for _, raw in decoded:
                image = save_image(raw, t.id)
                db.add(Attachment(ticket_id=t.id, worklog_id=w.id, uploader_id=user.id, **image))
            t.version += 1
            emit(db, t, "worklog_synced", user.id,
                 {"worklog_id": w.id, "client_at": fmt(action.client_at),
                  "server_at": fmt(w.server_at), "photo_count": len(decoded)},
                 key="sync:" + user.id + ":" + key)
            db.commit()
            results.append({"uuid": key, "status": "applied", "worklog_id": w.id,
                            "server_at": fmt(w.server_at), "server_version": t.version})
        except Exception:
            db.rollback()
            raise
    return {"results": results, "server_at": fmt(now())}

@router.post("/tickets/{ticket_id}/attachments")
async def upload_photo(ticket_id: str, file: UploadFile = File(...),
                       user: User = Depends(roles("engineer")),
                       db: Session = Depends(get_db)):
    t = ticket_for(db, user, ticket_id)
    if t.status not in ("accepted", "in_progress", "returned"):
        raise HTTPException(409, detail="Для заявки нельзя загружать фото")
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, detail="Лимит файла 10 МБ")
    image = save_image(raw, t.id)
    a = Attachment(ticket_id=t.id, uploader_id=user.id, **image)
    db.add(a)
    emit(db, t, "photo_uploaded", user.id, {"checksum": image["checksum"]})
    db.commit()
    return {"id": a.id, "checksum": a.checksum}

@router.get("/tickets/{ticket_id}/attachments")
def list_photos(ticket_id: str, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    t = ticket_for(db, user, ticket_id)
    return [{"id": a.id, "mime": a.mime, "size": a.size, "checksum": a.checksum}
            for a in db.scalars(select(Attachment).where(Attachment.ticket_id == t.id))]

@router.get("/attachments/{attachment_id}")
def get_photo(attachment_id: str, thumb: bool = False,
              user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    from fastapi.responses import Response
    a = db.get(Attachment, attachment_id)
    if not a:
        raise HTTPException(404, detail="Фото не найдено")
    ticket_for(db, user, a.ticket_id)
    return Response(read_image(a.thumb_key if thumb else a.storage_key),
                    media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
