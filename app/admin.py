"""Admin-only setup and workforce management. Every mutating action is audited."""
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .import_csv import import_assets, import_contracts, import_sites
from .models import AdminAudit, Asset, Contract, Organization, Shift, Site, SLAPolicy, Ticket, User
from .security import roles

router = APIRouter(prefix="/api/v1/admin", tags=["administration"])
PRIORITIES = ("P1", "P2", "P3", "P4")
KINDS = {"electrical", "hvac", "low_voltage"}
PAUSES = {"awaiting_access", "awaiting_parts"}

class PolicyInput(BaseModel):
    reaction_minutes: int = Field(ge=1, le=100800)
    resolution_minutes: int = Field(ge=1, le=100800)
    allowed_pauses: list[Literal["awaiting_access", "awaiting_parts"]] = Field(default_factory=list)
    week: dict[str, list[tuple[str, str]]]
    holidays: list[str] = Field(default_factory=list)
    @model_validator(mode="after")
    def calendar_valid(self):
        from .sla import parse_hhmm
        if not self.week or any(key not in {str(n) for n in range(7)} for key in self.week):
            raise ValueError("Укажите рабочие интервалы по дням недели 0..6")
        if not any(self.week.values()):
            raise ValueError("Рабочий календарь не может быть пустым")
        for spans in self.week.values():
            previous_end = None
            for start, finish in spans:
                if parse_hhmm(start) >= parse_hhmm(finish) or (
                    previous_end is not None and parse_hhmm(start) < previous_end
                ):
                    raise ValueError("Интервалы должны быть упорядочены и не пересекаться")
                previous_end = parse_hhmm(finish)
        for item in self.holidays:
            datetime.strptime(item, "%Y-%m-%d")
        return self

class ProfileInput(BaseModel):
    skills: list[Literal["electrical", "hvac", "low_voltage"]] = Field(min_length=1, max_length=3)
    area: str = Field(min_length=2, max_length=80)
    active: bool

class ShiftInput(BaseModel):
    starts_at: datetime
    ends_at: datetime
    @model_validator(mode="after")
    def verify_range(self):
        if (not self.starts_at.tzinfo or not self.ends_at.tzinfo
            or self.ends_at <= self.starts_at):
            raise ValueError("Смена должна содержать время с зоной и корректный интервал")
        return self

def audit(db, actor, action, target, data):
    db.add(AdminAudit(actor_id=actor.id, action=action, target=target, data=data))

@router.get("/summary")
def summary(admin: User = Depends(roles("admin")), db: Session = Depends(get_db)):
    return {
        "sites": db.scalar(select(func.count(Site.id))) or 0,
        "assets": db.scalar(select(func.count(Asset.id))) or 0,
        "contracts": db.scalar(select(func.count(Contract.id))) or 0,
        "policies": db.scalar(select(func.count(SLAPolicy.id))) or 0,
        "engineers": db.scalar(select(func.count(User.id)).where(User.role == "engineer")) or 0,
    }

@router.get("/contracts/{site_id}")
def contract_detail(site_id: str, _: User = Depends(roles("admin")),
                    db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if not site:
        raise HTTPException(404, detail="Объект не найден")
    contract = db.scalar(select(Contract).where(Contract.site_id == site_id))
    if not contract:
        return {"site_id": site_id, "timezone": site.timezone, "policies": {}, "calendar": None}
    policies = {}
    for priority, policy_id in contract.policy_ids.items():
        p = db.get(SLAPolicy, policy_id)
        if p:
            policies[priority] = {
                "id": p.id, "name": p.name, "reaction_minutes": p.reaction_minutes,
                "resolution_minutes": p.resolution_minutes,
                "allowed_pauses": p.allowed_pauses
            }
    return {"site_id": site_id, "timezone": site.timezone,
            "policies": policies, "calendar": contract.calendar}

@router.put("/contracts/{site_id}/policies/{priority}")
def put_policy(site_id: str, priority: Literal["P1", "P2", "P3", "P4"],
               data: PolicyInput, actor: User = Depends(roles("admin")),
               db: Session = Depends(get_db)):
    site = db.get(Site, site_id)
    if not site:
        raise HTTPException(404, detail="Объект не найден")
    contract = db.scalar(select(Contract).where(Contract.site_id == site_id).with_for_update())
    new_calendar = {"timezone": site.timezone, "week": data.week, "holidays": sorted(set(data.holidays))}
    if contract is not None and contract.calendar != new_calendar:
        # Calendar changes are explicit and affect future tickets only via snapshots.
        pass
    if contract is None:
        contract = Contract(site_id=site_id, policy_ids={}, calendar=new_calendar)
        db.add(contract)
    else:
        contract.calendar = new_calendar
    old_id = contract.policy_ids.get(priority)
    new_policy = SLAPolicy(name="%s %s / редакция" % (site.name, priority),
                           priority=priority, reaction_minutes=data.reaction_minutes,
                           resolution_minutes=data.resolution_minutes,
                           allowed_pauses=list(dict.fromkeys(data.allowed_pauses)))
    db.add(new_policy)
    db.flush()
    contract.policy_ids = {**contract.policy_ids, priority: new_policy.id}
    audit(db, actor, "policy_version_created", site_id + "/" + priority,
          {"previous_id": old_id, "new_id": new_policy.id,
           "calendar": new_calendar, "targets": {
               "reaction_minutes": data.reaction_minutes,
               "resolution_minutes": data.resolution_minutes}})
    db.commit()
    return {"site_id": site_id, "priority": priority,
            "previous_policy_id": old_id, "policy_id": new_policy.id}

@router.post("/import/{kind}")
async def upload_csv(kind: Literal["sites", "assets", "contracts"],
                     dry_run: bool = False, file: UploadFile = File(...),
                     actor: User = Depends(roles("admin")),
                     db: Session = Depends(get_db)):
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, detail="CSV превышает 2 МиБ")
    if not raw:
        raise HTTPException(422, detail="CSV пуст")
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, detail="Требуется CSV в UTF-8")
    filename = None
    try:
        with tempfile.NamedTemporaryFile(prefix="fieldpulse-import-", suffix=".csv", delete=False) as temp:
            temp.write(raw)
            filename = temp.name
        handler = {"sites": import_sites, "assets": import_assets, "contracts": import_contracts}[kind]
        changed = handler(db, filename)
        if dry_run:
            db.rollback()
        else:
            audit(db, actor, "csv_import", kind, {"created_or_updated": changed})
            db.commit()
        return {"kind": kind, "dry_run": dry_run, "created_or_updated": changed}
    except (ValueError, KeyError, OverflowError) as exc:
        db.rollback()
        raise HTTPException(422, detail="CSV: " + str(exc)[:300])
    finally:
        if filename is not None:
            Path(filename).unlink(missing_ok=True)

@router.get("/engineers")
def engineers(_: User = Depends(roles("admin")), db: Session = Depends(get_db)):
    staff = db.scalars(select(User).where(User.role == "engineer").order_by(User.name)).all()
    result = []
    for u in staff:
        shifts = db.scalars(select(Shift).where(Shift.engineer_id == u.id).order_by(Shift.starts_at.desc()).limit(10)).all()
        result.append({"id": u.id, "name": u.name, "area": u.area, "skills": u.skills,
                       "active": u.active, "shifts": [
                           {"id": s.id, "starts_at": s.starts_at.isoformat(),
                            "ends_at": s.ends_at.isoformat()} for s in shifts]})
    return result

@router.put("/engineers/{engineer_id}/profile")
def profile(engineer_id: str, data: ProfileInput, actor: User = Depends(roles("admin")),
            db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.id == engineer_id, User.role == "engineer").with_for_update())
    if u is None:
        raise HTTPException(404, detail="Инженер не найден")
    before = {"skills": u.skills, "area": u.area, "active": u.active}
    u.skills, u.area, u.active = list(dict.fromkeys(data.skills)), data.area, data.active
    audit(db, actor, "engineer_profile_updated", engineer_id,
          {"before": before, "after": {"skills": u.skills, "area": u.area, "active": u.active}})
    db.commit()
    return {"id": u.id, "skills": u.skills, "area": u.area, "active": u.active}

@router.post("/engineers/{engineer_id}/shifts", status_code=201)
def add_shift(engineer_id: str, data: ShiftInput, actor: User = Depends(roles("admin")),
              db: Session = Depends(get_db)):
    u = db.get(User, engineer_id)
    if u is None or u.role != "engineer":
        raise HTTPException(404, detail="Инженер не найден")
    overlaps = db.scalar(select(Shift.id).where(
        Shift.engineer_id == engineer_id,
        Shift.starts_at < data.ends_at, Shift.ends_at > data.starts_at).limit(1))
    if overlaps:
        raise HTTPException(409, detail="Смена пересекается с существующей")
    s = Shift(engineer_id=engineer_id, starts_at=data.starts_at, ends_at=data.ends_at)
    db.add(s)
    db.flush()
    audit(db, actor, "shift_created", engineer_id,
          {"shift_id": s.id, "starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat()})
    db.commit()
    return {"id": s.id, "engineer_id": engineer_id}

@router.get("/audit")
def audit_log(limit: int = 100, _: User = Depends(roles("admin")),
              db: Session = Depends(get_db)):
    if limit < 1 or limit > 500:
        raise HTTPException(422, detail="limit 1..500")
    events = db.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(limit)).all()
    return [{"id": e.id, "actor_id": e.actor_id, "action": e.action, "target": e.target,
             "data": e.data, "created_at": e.created_at.isoformat()} for e in events]
