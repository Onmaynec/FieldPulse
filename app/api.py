"""Versioned FieldPulse HTTP API. Every entity lookup is authorized server-side."""
import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .db import get_db
from .models import (
    Act, Asset, Attachment, AuthAttempt, BoardEvent, Contract, Event, Idempotency, Organization,
    Outbox, Pause, Shift, Site, SLAPolicy, Ticket, User, Worklog, now
)
from .security import (
    access_site, access_ticket, current_user, roles, signer, hash_password,
    verify_password
)
from .sla import aware, deadline, snapshot

router = APIRouter(prefix="/api/v1")
OPEN = ("new", "triage", "assigned", "accepted", "in_progress", "approval", "returned")
STAFF = ("dispatcher", "manager", "admin")
TEMPLATES = {
    "electrical": ["isolation", "voltage", "result"],
    "hvac": ["filter", "pressure", "result"],
    "low_voltage": ["connection", "signal", "result"],
}

class LoginInput(BaseModel):
    email: str
    password: str

class TicketInput(BaseModel):
    site_id: str
    asset_id: str
    priority: Literal["P1", "P2", "P3", "P4"]
    kind: str = Field(min_length=2, max_length=40)
    description: str = Field(min_length=8, max_length=2000)

class VersionInput(BaseModel):
    version: int = Field(ge=1)

class AssignInput(VersionInput):
    engineer_id: str

class CommentInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    internal: bool = False

class PauseInput(VersionInput):
    reason: str

class DecisionInput(BaseModel):
    version: int = Field(ge=1)
    decision: Literal["accepted", "rejected"]
    reason: str = Field(default="", max_length=1000)

def fmt(dt):
    return aware(dt).isoformat() if dt else None

def person(u):
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role, "area": u.area, "skills": u.skills}

def serialize_ticket(db, t):
    site = db.get(Site, t.site_id)
    asset = db.get(Asset, t.asset_id)
    assignee = db.get(User, t.assignee_id) if t.assignee_id else None
    return {
        "id": t.id, "site_id": t.site_id, "site": site.name, "timezone": site.timezone,
        "asset_id": t.asset_id, "asset": asset.name, "asset_kind": asset.kind,
        "priority": t.priority, "kind": t.kind, "description": t.description,
        "status": t.status, "version": t.version, "assignee_id": t.assignee_id,
        "assignee": assignee.name if assignee else None, "duplicate_of": t.duplicate_of,
        "created_at": fmt(t.created_at), "reaction_deadline": fmt(t.reaction_deadline),
        "resolution_deadline": fmt(t.resolution_deadline),
        "accepted_at": fmt(t.accepted_at), "started_at": fmt(t.started_at),
        "finished_at": fmt(t.finished_at), "closed_at": fmt(t.closed_at),
        "sla": t.policy_snapshot
    }

def require_version(t, version):
    if t.version != version:
        raise HTTPException(409, detail={"code": "VERSION_CONFLICT", "serverVersion": t.version, "serverStatus": t.status})

def emit(db, t, kind, actor=None, data=None, key=None):
    db.add(Event(ticket_id=t.id, actor_id=actor, event_key=key or str(uuid.uuid4()),
                 type=kind, data=data or {}))
    db.add(BoardEvent(org_id=t.org_id, ticket_id=t.id, kind=kind))

def ticket_for(db, user, ticket_id):
    ticket = ticket_id if isinstance(ticket_id, Ticket) else db.get(Ticket, ticket_id)
    return access_ticket(db, user, ticket)

def get_contract(db, site_id):
    contract = db.scalar(select(Contract).where(Contract.site_id == site_id))
    if not contract:
        raise HTTPException(422, detail="Для объекта не настроен договор")
    return contract

def get_policy(db, contract, priority):
    p = db.get(SLAPolicy, contract.policy_ids.get(priority))
    if not p or p.priority != priority:
        raise HTTPException(422, detail="Для приоритета не назначена политика SLA")
    return p

def template(db, t):
    a = db.get(Asset, t.asset_id)
    return TEMPLATES.get(a.kind, ["inspection", "result"])

def require_status(t, *valid):
    if t.status not in valid:
        raise HTTPException(409, detail={"code": "INVALID_TRANSITION", "status": t.status, "allowedFrom": valid})

def idempotency(db, user, route, key, body, create=None):
    if not key or len(key) > 80:
        raise HTTPException(400, detail="Требуется заголовок Idempotency-Key до 80 символов")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    if db.bind.dialect.name == "postgresql":
        lock_key = int.from_bytes(hashlib.sha256((user.id + route + key).encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    old = db.scalar(select(Idempotency).where(
        Idempotency.actor_id == user.id, Idempotency.route == route,
        Idempotency.client_uuid == key))
    if old:
        if old.request_hash != digest:
            raise HTTPException(409, detail="Ключ идемпотентности использован для другого запроса")
        return old.response
    result = create()
    db.add(Idempotency(actor_id=user.id, route=route, client_uuid=key,
                       request_hash=digest, response=result))
    return result

@router.post("/auth/login")
def login(data: LoginInput, response: Response, request: Request, db: Session = Depends(get_db)):
    email = data.email.strip().lower()
    remote = request.client.host if request.client else "unknown"
    fingerprint = hashlib.sha256((remote + "|" + email).encode()).hexdigest()
    cutoff = now() - timedelta(minutes=15)
    db.query(AuthAttempt).filter(AuthAttempt.created_at < cutoff).delete(synchronize_session=False)
    failures = db.scalar(select(func.count(AuthAttempt.id)).where(AuthAttempt.fingerprint == fingerprint)) or 0
    if failures >= 5:
        db.commit()
        raise HTTPException(429, detail="Слишком много попыток. Повторите вход через 15 минут")
    u = db.scalar(select(User).where(User.email == email))
    if not u or not u.active or not verify_password(data.password, u.password_hash):
        db.add(AuthAttempt(fingerprint=fingerprint, created_at=now()))
        db.commit()
        raise HTTPException(401, detail="Неверный логин или пароль")
    db.query(AuthAttempt).filter(AuthAttempt.fingerprint == fingerprint).delete(synchronize_session=False)
    db.commit()
    secure = os.getenv("COOKIE_SECURE", "0") == "1"
    response.set_cookie("fp_session", signer().dumps(u.id), max_age=43200,
                        httponly=True, secure=secure, samesite="lax", path="/")
    response.set_cookie("fp_csrf", secrets.token_urlsafe(24), max_age=43200,
                        httponly=False, secure=secure, samesite="lax", path="/")
    return person(u)

@router.post("/auth/logout")
def logout(response: Response, _: User = Depends(current_user)):
    response.delete_cookie("fp_session", path="/")
    response.delete_cookie("fp_csrf", path="/")
    return {"ok": True}

@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return person(user)

@router.get("/sites")
def sites(user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Site).order_by(Site.name)
    if user.role == "client":
        q = q.where(Site.org_id == user.org_id)
    elif user.role == "engineer":
        q = q.join(Ticket, Ticket.site_id == Site.id).where(Ticket.assignee_id == user.id).distinct()
    return [{"id": s.id, "name": s.name, "city": s.city, "area": s.area,
             "timezone": s.timezone} for s in db.scalars(q).all()]

@router.get("/assets")
def assets(site_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if user.role == "engineer":
        if not db.scalar(select(Ticket.id).where(Ticket.site_id == site_id, Ticket.assignee_id == user.id).limit(1)):
            raise HTTPException(404, detail="Объект не найден")
    else:
        access_site(db, user, site)
    return [{"id": a.id, "site_id": a.site_id, "name": a.name, "kind": a.kind}
            for a in db.scalars(select(Asset).where(Asset.site_id == site_id)).all()]

@router.get("/tickets")
def tickets(status: str | None = None, limit: int = Query(100, ge=1, le=500),
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Ticket).order_by(Ticket.created_at.desc()).limit(limit)
    if user.role == "client":
        q = q.where(Ticket.org_id == user.org_id)
    elif user.role == "engineer":
        q = q.where(Ticket.assignee_id == user.id)
    if status:
        q = q.where(Ticket.status == status)
    return [serialize_ticket(db, t) for t in db.scalars(q)]

@router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: str, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    t = ticket_for(db, user, ticket_id)
    return serialize_ticket(db, t)

@router.post("/tickets", status_code=201)
def create_ticket(data: TicketInput, request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    if user.role not in ("client", "dispatcher", "manager", "admin"):
        raise HTTPException(403, detail="Создание заявок недоступно")
    site = access_site(db, user, db.get(Site, data.site_id))
    asset = db.get(Asset, data.asset_id)
    if not asset or asset.site_id != site.id:
        raise HTTPException(422, detail="Оборудование не принадлежит объекту")
    contract = get_contract(db, site.id)
    policy = get_policy(db, contract, data.priority)
    s = snapshot(site, contract, policy)
    body = data.model_dump()
    def do_create():
        earlier = db.scalar(select(Ticket.id).where(
            Ticket.site_id == site.id, Ticket.asset_id == asset.id,
            Ticket.kind == data.kind,
            Ticket.created_at >= now() - timedelta(hours=24)
        ).order_by(Ticket.created_at.desc()).limit(1))
        created = now()
        t = Ticket(org_id=site.org_id, site_id=site.id, asset_id=asset.id,
                   creator_id=user.id, priority=data.priority, kind=data.kind,
                   description=data.description, policy_snapshot=s,
                   reaction_deadline=deadline(created, policy.reaction_minutes, s["calendar"]),
                   resolution_deadline=deadline(created, policy.resolution_minutes, s["calendar"]),
                   created_at=created, duplicate_of=earlier, status="new")
        db.add(t)
        db.flush()
        emit(db, t, "ticket_created", user.id, {"priority": data.priority, "duplicate_of": earlier})
        return serialize_ticket(db, t)
    result = idempotency(db, user, "create_ticket", request.headers.get("Idempotency-Key"), body, do_create)
    db.commit()
    return result

@router.get("/tickets/{ticket_id}/timeline")
def timeline(ticket_id: str, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    t = ticket_for(db, user, ticket_id)
    q = select(Event).where(Event.ticket_id == t.id).order_by(Event.id)
    if user.role == "client":
        q = q.where(Event.type != "internal_comment")
    return [{"type": e.type, "at": fmt(e.at), "actor_id": e.actor_id,
             "data": e.data} for e in db.scalars(q)]

@router.post("/tickets/{ticket_id}/comments")
def comment(ticket_id: str, data: CommentInput, user: User = Depends(current_user),
            db: Session = Depends(get_db)):
    t = ticket_for(db, user, ticket_id)
    if user.role == "client" and data.internal:
        raise HTTPException(403, detail="Нет доступа к внутренним комментариям")
    emit(db, t, "internal_comment" if data.internal else "comment",
         user.id, {"text": data.text})
    db.commit()
    return {"ok": True}

@router.post("/tickets/{ticket_id}/triage")
def triage(ticket_id: str, data: VersionInput, user: User = Depends(roles(*STAFF)),
           db: Session = Depends(get_db)):
    t = ticket_for(db, user, ticket_id)
    require_version(t, data.version)
    require_status(t, "new")
    t.status, t.version = "triage", t.version + 1
    emit(db, t, "triage", user.id)
    db.commit()
    return serialize_ticket(db, t)

def rank_engineers(db, t):
    site = db.get(Site, t.site_id)
    asset = db.get(Asset, t.asset_id)
    current = now()
    candidates = db.scalars(select(User).where(User.role == "engineer", User.active.is_(True))).all()
    result = []
    for u in candidates:
        on_shift = db.scalar(select(Shift.id).where(
            Shift.engineer_id == u.id,
            Shift.starts_at <= current, Shift.ends_at > current).limit(1)) is not None
        load = db.scalar(select(func.count(Ticket.id)).where(
            Ticket.assignee_id == u.id,
            Ticket.status.in_(("assigned", "accepted", "in_progress", "returned")))) or 0
        skill = asset.kind in (u.skills or [])
        area = site.area == u.area
        eligible = skill and on_shift and load < 8
        weights = {"skill": 50 if skill else 0, "shift": 25 if on_shift else 0,
                   "area": 15 if area else 0, "load": round(10 * max(0, 1 - load / 8), 2)}
        result.append({"id": u.id, "name": u.name, "area": u.area,
                       "skills": u.skills, "on_shift": on_shift, "load": load,
                       "eligible": eligible, "score": sum(weights.values()),
                       "weights": weights,
                       "reason": "Допустим" if eligible else
                       ("Нет нужной компетенции" if not skill else
                        "Нет активной смены" if not on_shift else "Слишком много задач")})
    return sorted(result, key=lambda r: (r["eligible"], r["score"]), reverse=True)

@router.get("/engineers/suggestions")
def suggestions(ticket_id: str, user: User = Depends(roles(*STAFF)),
                db: Session = Depends(get_db)):
    t = ticket_for(db, user, ticket_id)
    return rank_engineers(db, t)

@router.post("/tickets/{ticket_id}/assign")
def assign(ticket_id: str, data: AssignInput, user: User = Depends(roles(*STAFF)),
           db: Session = Depends(get_db)):
    t = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    t = ticket_for(db, user, t)
    require_version(t, data.version)
    require_status(t, "new", "triage", "returned")
    db.scalar(select(User).where(User.id == data.engineer_id).with_for_update())
    candidate = next((r for r in rank_engineers(db, t) if r["id"] == data.engineer_id), None)
    if not candidate or not candidate["eligible"]:
        raise HTTPException(422, detail={"code": "ENGINEER_INELIGIBLE", "candidate": candidate})
    t.assignee_id, t.status, t.version = data.engineer_id, "assigned", t.version + 1
    emit(db, t, "assigned", user.id, {"engineer_id": data.engineer_id})
    db.commit()
    return serialize_ticket(db, t)

@router.post("/tickets/{ticket_id}/accept")
def accept(ticket_id: str, data: VersionInput, user: User = Depends(roles("engineer")),
           db: Session = Depends(get_db)):
    t = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    t = ticket_for(db, user, t)
    require_version(t, data.version)
    require_status(t, "assigned")
    t.accepted_at, t.status, t.version = now(), "accepted", t.version + 1
    emit(db, t, "accepted", user.id, {"reaction_at": fmt(t.accepted_at)})
    db.commit()
    return serialize_ticket(db, t)

@router.post("/tickets/{ticket_id}/start")
def start(ticket_id: str, data: VersionInput, user: User = Depends(roles("engineer")),
          db: Session = Depends(get_db)):
    t = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    t = ticket_for(db, user, t)
    require_version(t, data.version)
    require_status(t, "accepted", "returned")
    if not t.started_at:
        t.started_at = now()
    t.status, t.version = "in_progress", t.version + 1
    emit(db, t, "work_started", user.id)
    db.commit()
    return serialize_ticket(db, t)

@router.post("/tickets/{ticket_id}/cancel")
def cancel(ticket_id: str, data: VersionInput, user: User = Depends(roles(*STAFF)),
           db: Session = Depends(get_db)):
    t = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    t = ticket_for(db, user, t)
    require_version(t, data.version)
    require_status(t, *[s for s in OPEN if s != "approval"])
    t.status, t.version = "cancelled", t.version + 1
    emit(db, t, "cancelled", user.id)
    db.commit()
    return serialize_ticket(db, t)

@router.post("/tickets/{ticket_id}/pause")
def pause(ticket_id: str, data: PauseInput, user: User = Depends(roles("dispatcher", "manager")),
          db: Session = Depends(get_db)):
    t = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    t = ticket_for(db, user, t)
    require_version(t, data.version)
    require_status(t, "assigned", "accepted", "in_progress")
    if data.reason not in t.policy_snapshot["allowed_pauses"]:
        raise HTTPException(422, detail="Причина не разрешена договором")
    if db.scalar(select(Pause).where(Pause.ticket_id == t.id, Pause.ended_at.is_(None))):
        raise HTTPException(409, detail="Пауза уже активна")
    db.add(Pause(ticket_id=t.id, reason=data.reason, actor_id=user.id, started_at=now()))
    t.version += 1
    emit(db, t, "sla_pause_started", user.id, {"reason": data.reason})
    db.commit()
    return serialize_ticket(db, t)

@router.post("/tickets/{ticket_id}/resume")
def resume(ticket_id: str, data: VersionInput, user: User = Depends(roles("dispatcher", "manager")),
           db: Session = Depends(get_db)):
    from .sla import apply_pause
    t = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    t = ticket_for(db, user, t)
    require_version(t, data.version)
    active = db.scalar(select(Pause).where(Pause.ticket_id == t.id, Pause.ended_at.is_(None)))
    if not active:
        raise HTTPException(409, detail="Нет активной паузы")
    active.ended_at = now()
    old = t.resolution_deadline
    t.resolution_deadline = apply_pause(old, active.started_at, active.ended_at,
                                       t.policy_snapshot["calendar"])
    t.version += 1
    emit(db, t, "sla_pause_ended", user.id,
         {"reason": active.reason, "started_at": fmt(active.started_at),
          "ended_at": fmt(active.ended_at), "previous_deadline": fmt(old),
          "new_deadline": fmt(t.resolution_deadline)})
    db.commit()
    return serialize_ticket(db, t)

@router.get("/tickets/{ticket_id}/checklist")
def checklist(ticket_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"required": template(db, ticket_for(db, user, ticket_id))}

@router.get("/tickets/{ticket_id}/worklogs")
def worklogs(ticket_id: str, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    t = ticket_for(db, user, ticket_id)
    return [{"id": w.id, "summary": w.summary, "checklist": w.checklist,
             "materials": w.materials, "client_at": fmt(w.client_at),
             "server_at": fmt(w.server_at)}
            for w in db.scalars(select(Worklog).where(Worklog.ticket_id == t.id).order_by(Worklog.server_at))]

@router.post("/tickets/{ticket_id}/submit")
def submit(ticket_id: str, data: VersionInput, user: User = Depends(roles("engineer")),
           db: Session = Depends(get_db)):
    t = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    t = ticket_for(db, user, t)
    require_version(t, data.version)
    require_status(t, "in_progress")
    logs = db.scalars(select(Worklog).where(Worklog.ticket_id == t.id)).all()
    checklist_values = {}
    for w in logs:
        checklist_values.update(w.checklist or {})
    missing = [name for name in template(db, t) if checklist_values.get(name) is not True]
    photo_count = db.scalar(select(func.count(Attachment.id)).where(Attachment.ticket_id == t.id))
    if missing or not photo_count:
        raise HTTPException(422, detail={"code": "CHECKLIST_INCOMPLETE",
                                         "missing": missing, "photo_required": not bool(photo_count)})
    previous = db.scalar(select(Act).where(Act.ticket_id == t.id).order_by(Act.version.desc()))
    revision = previous.version + 1 if previous else 1
    content = {
        "work": [{"summary": w.summary, "client_at": fmt(w.client_at),
                  "server_at": fmt(w.server_at)} for w in logs],
        "materials": [m for w in logs for m in (w.materials or [])],
        "engineer_id": user.id, "started_at": fmt(t.started_at), "finished_at": fmt(now()),
        "attachment_count": photo_count
    }
    if previous and previous.status == "draft":
        previous.status, previous.content = "pending", content
        a = previous
    else:
        a = Act(ticket_id=t.id, version=revision, status="pending", content=content)
        db.add(a)
    t.finished_at = t.finished_at or now()
    t.status, t.version = "approval", t.version + 1
    db.flush()
    emit(db, t, "act_submitted", user.id, {"act_id": a.id, "act_version": a.version})
    db.commit()
    return {"id": a.id, "version": a.version, "status": a.status}

@router.get("/acts")
def acts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Act).join(Ticket, Ticket.id == Act.ticket_id).order_by(Act.created_at.desc()).limit(200)
    if user.role == "client":
        q = q.where(Ticket.org_id == user.org_id)
    elif user.role == "engineer":
        q = q.where(Ticket.assignee_id == user.id)
    return [serialize_act(a) for a in db.scalars(q)]

def serialize_act(a):
    return {"id": a.id, "ticket_id": a.ticket_id, "version": a.version,
            "status": a.status, "content": a.content, "reason": a.reason,
            "created_at": fmt(a.created_at), "decided_at": fmt(a.decided_at)}

@router.get("/acts/{act_id}")
def act_detail(act_id: str, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    a = db.get(Act, act_id)
    if not a:
        raise HTTPException(404, detail="Акт не найден")
    ticket_for(db, user, a.ticket_id)
    return serialize_act(a)

@router.post("/acts/{act_id}/decision")
def act_decision(act_id: str, data: DecisionInput,
                 user: User = Depends(roles("client", "manager")),
                 db: Session = Depends(get_db)):
    a = db.scalar(select(Act).where(Act.id == act_id).with_for_update())
    if not a:
        raise HTTPException(404, detail="Акт не найден")
    t = db.scalar(select(Ticket).where(Ticket.id == a.ticket_id).with_for_update())
    ticket_for(db, user, t.id)
    if a.version != data.version or a.status != "pending" or t.status != "approval":
        raise HTTPException(409, detail="Редакция уже обработана или устарела")
    latest = db.scalar(select(func.max(Act.version)).where(Act.ticket_id == t.id))
    if latest != a.version:
        raise HTTPException(409, detail="Существует новая редакция")
    if data.decision == "rejected" and not data.reason.strip():
        raise HTTPException(422, detail="Укажите причину возврата")
    a.status, a.reason, a.decided_at, a.decided_by = data.decision, data.reason.strip(), now(), user.id
    if data.decision == "accepted":
        t.status, t.closed_at = "closed", now()
        emit(db, t, "act_accepted", user.id, {"act_version": a.version})
    else:
        t.status = "returned"
        db.add(Act(ticket_id=t.id, version=a.version + 1, status="draft",
                   content={"previous_act": a.id, "reason": a.reason}))
        emit(db, t, "act_rejected", user.id,
             {"act_version": a.version, "reason": a.reason, "next_version": a.version + 1})
    t.version += 1
    db.commit()
    return serialize_act(a)

@router.get("/sla/violations")
def violations(user: User = Depends(roles(*STAFF)), db: Session = Depends(get_db)):
    q = select(Event).where(Event.type.in_(("sla_reaction_breached", "sla_resolution_breached"))).order_by(Event.id.desc()).limit(500)
    return [{"ticket_id": e.ticket_id, "type": e.type, "at": fmt(e.at), "data": e.data}
            for e in db.scalars(q)]

def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * fraction
    a = int(k)
    b = min(len(values) - 1, a + 1)
    portion = k - a
    return round(values[a] * (1 - portion) + values[b] * portion, 2)

@router.get("/reports/overview")
def overview(user: User = Depends(roles(*STAFF)), db: Session = Depends(get_db)):
    items = db.scalars(select(Ticket)).all()
    reactions = [round((aware(t.accepted_at) - aware(t.created_at)).total_seconds() / 60, 2)
                 for t in items if t.accepted_at]
    breaching = db.scalars(select(Event.ticket_id).where(
        Event.type.in_(("sla_reaction_breached", "sla_resolution_breached"))).distinct()).all()
    repeated = sum(bool(t.duplicate_of) for t in items)
    loads = db.execute(select(User.name, func.count(Ticket.id)).join(
        Ticket, Ticket.assignee_id == User.id).where(
        Ticket.status.in_(("assigned","accepted","in_progress","returned")))
        .group_by(User.id).order_by(func.count(Ticket.id).desc()).limit(10)).all()
    return {
        "total": len(items), "open": sum(t.status in OPEN for t in items),
        "closed": sum(t.status == "closed" for t in items),
        "reaction_p50_minutes": percentile(reactions, .5),
        "reaction_p95_minutes": percentile(reactions, .95),
        "breached_count": len(breaching),
        "breached_percent": round(100 * len(breaching) / len(items), 2) if items else 0,
        "repeat_count": repeated,
        "repeat_percent": round(100 * repeated / len(items), 2) if items else 0,
        "load": [{"name": name, "count": count} for name, count in loads]
    }

@router.get("/board/events")
async def board_events(request: Request, user: User = Depends(current_user)):
    import asyncio
    from fastapi.responses import StreamingResponse
    from .db import SessionLocal
    if user.role == "client":
        raise HTTPException(403, detail="Доступ к диспетчерской закрыт")
    since = int(request.headers.get("Last-Event-ID") or request.query_params.get("since") or 0)
    async def stream():
        nonlocal since
        while not await request.is_disconnected():
            with SessionLocal() as db:
                q = select(BoardEvent).where(BoardEvent.id > since).order_by(BoardEvent.id).limit(100)
                if user.role == "engineer":
                    q = q.join(Ticket, Ticket.id == BoardEvent.ticket_id).where(Ticket.assignee_id == user.id)
                for event in db.scalars(q):
                    since = event.id
                    yield "id: %s\nevent: board\ndata: %s\n\n" % (event.id, json.dumps(
                        {"ticket_id": event.ticket_id, "kind": event.kind}))
            yield ": keepalive\n\n"
            await asyncio.sleep(0.7)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.get("/notifications")
def notifications(user: User = Depends(roles(*STAFF)), db: Session = Depends(get_db)):
    return [{"recipient": x.recipient, "subject": x.subject, "body": x.body, "at": fmt(x.created_at)}
            for x in db.scalars(select(Outbox).order_by(Outbox.id.desc()).limit(100))]
