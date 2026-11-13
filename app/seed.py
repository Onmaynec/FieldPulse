"""Deterministic demo fixture: 120 sites, 600 assets, 45 engineers, 15 policies and 2000 tickets."""
import os
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .db import SessionLocal
from .models import (
    Asset, Contract, Event, Organization, Shift, Site, SLAPolicy,
    Ticket, User, Worklog, now, uid
)
from .security import hash_password
from .sla import deadline, snapshot

def seed(db):
    if db.scalar(select(Organization.id).limit(1)):
        print("База уже содержит данные; seed не изменил существующие записи")
        return
    rnd = random.Random(2402)
    service = Organization(id=uid(), name="ГородТех Сервис")
    alpha = Organization(id=uid(), name="Бизнес-центры Север")
    beta = Organization(id=uid(), name="Коммерческие объекты Юг")
    db.add_all([service, alpha, beta])
    password = hash_password(os.getenv("DEMO_PASSWORD", "FieldPulseDemo!2026"))
    users = []
    for name, email, role, org in [
        ("Администратор", "admin@fieldpulse.local", "admin", service),
        ("Елена Диспетчер", "dispatcher@fieldpulse.local", "dispatcher", service),
        ("Андрей Руководитель", "manager@fieldpulse.local", "manager", service),
        ("Ольга Заказчик", "client-a@fieldpulse.local", "client", alpha),
        ("Игорь Заказчик", "client-b@fieldpulse.local", "client", beta),
    ]:
        u = User(id=uid(), org_id=org.id, name=name, email=email,
                 role=role, password_hash=password, skills=[], area="")
        users.append(u)
    engineer_skills = ["electrical", "hvac", "low_voltage"]
    regions = ["Центр", "Север", "Юг", "Запад", "Восток"]
    for i in range(45):
        u = User(id=uid(), org_id=service.id, name="Инженер %02d" % (i+1),
                 email="engineer%d@fieldpulse.local" % (i+1),
                 role="engineer", password_hash=password,
                 area=regions[i % len(regions)],
                 skills=[engineer_skills[i % 3], engineer_skills[(i+1) % 3]])
        users.append(u)
    db.add_all(users)
    engineers = users[5:]
    current = now()
    for u in engineers:
        db.add(Shift(engineer_id=u.id, starts_at=current - timedelta(days=1),
                     ends_at=current + timedelta(days=1)))
    policies = []
    for i in range(15):
        pr = ["P1", "P2", "P3", "P4"][i % 4]
        idx = i // 4
        reaction = [30, 60, 180, 480][i % 4] + idx * 10
        resolution = [240, 480, 1440, 2880][i % 4] + idx * 30
        p = SLAPolicy(id=uid(), name="Пакет %02d / %s" % (i+1, pr),
                      priority=pr, reaction_minutes=reaction,
                      resolution_minutes=resolution,
                      allowed_pauses=["awaiting_access", "awaiting_parts"])
        policies.append(p)
    db.add_all(policies)
    by_priority = {}
    for p in policies:
        by_priority.setdefault(p.priority, []).append(p)
    sites = []
    assets = []
    contracts = []
    for i in range(120):
        site = Site(id=uid(), org_id=alpha.id if i < 60 else beta.id,
                    name="Бизнес-центр %03d" % (i+1),
                    city="Москва" if i < 60 else "Казань",
                    area=regions[i % 5], timezone="Europe/Moscow")
        sites.append(site)
        week = {str(d): [["00:00","23:59"]] for d in range(7)} if i % 5 == 0 else {
            str(d): [["09:00","18:00"]] for d in range(5)}
        policy_ids = {pr: rnd.choice(values).id for pr, values in by_priority.items()}
        contracts.append(Contract(site_id=site.id, policy_ids=policy_ids,
                                  calendar={"timezone": site.timezone, "week": week, "holidays": []}))
        for j in range(5):
            kind = engineer_skills[j % 3]
            assets.append(Asset(id=uid(), site_id=site.id,
                                name="%s / узел %d" % ({
                                    "electrical":"Электрощит", "hvac":"Вентустановка",
                                    "low_voltage":"Контроллер"}[kind], j+1),
                                kind=kind))
    db.add_all(sites + assets + contracts)
    db.flush()
    assets_by_site = {s.id: [a for a in assets if a.site_id == s.id] for s in sites}
    contract_by_site = {c.site_id: c for c in contracts}
    policy_by_id = {p.id: p for p in policies}
    for i in range(2000):
        site = sites[i % len(sites)]
        asset = rnd.choice(assets_by_site[site.id])
        priority = rnd.choices(["P1","P2","P3","P4"], [8,24,45,23])[0]
        policy = policy_by_id[contract_by_site[site.id].policy_ids[priority]]
        shot = snapshot(site, contract_by_site[site.id], policy)
        created = current - timedelta(minutes=rnd.randint(0, 90 * 24 * 60))
        delta = timedelta(minutes=rnd.randint(1, 900))
        status = rnd.choices(
            ["new", "triage", "assigned", "accepted", "in_progress", "approval", "closed"],
            [8, 9, 15, 14, 22, 10, 22]
        )[0]
        engineer = rnd.choice([e for e in engineers if asset.kind in e.skills])
        t = Ticket(id=uid(), org_id=site.org_id, site_id=site.id, asset_id=asset.id,
                   creator_id=users[3].id if site.org_id == alpha.id else users[4].id,
                   assignee_id=engineer.id if status not in ("new", "triage") else None,
                   priority=priority, kind="repair", description="Демонстрационная заявка #%d: проверка оборудования" % (i+1),
                   status=status, version=1, policy_snapshot=shot, created_at=created,
                   reaction_deadline=deadline(created, policy.reaction_minutes, shot["calendar"]),
                   resolution_deadline=deadline(created, policy.resolution_minutes, shot["calendar"]),
                   accepted_at=created + delta if status in ("accepted", "in_progress", "approval", "closed") else None,
                   started_at=created + delta + timedelta(minutes=5) if status in ("in_progress", "approval", "closed") else None,
                   finished_at=created + delta + timedelta(minutes=60) if status in ("approval", "closed") else None,
                   closed_at=created + delta + timedelta(minutes=90) if status == "closed" else None)
        db.add(t)
        db.add(Event(ticket_id=t.id, actor_id=t.creator_id, event_key="seed:"+str(i),
                     type="ticket_created", at=created, data={"seed": True}))
        if i % 9 == 0 and i > 120:
            t.duplicate_of = "demo-reference"
    db.commit()
    print("Seed: 120 объектов, 600 единиц оборудования, 45 инженеров, 15 SLA, 2000 заявок")
    print("Демо: admin@fieldpulse.local / dispatcher@fieldpulse.local / manager@fieldpulse.local")
    print("client-a@fieldpulse.local / client-b@fieldpulse.local / engineer1@fieldpulse.local")
    print("Пароль: значение DEMO_PASSWORD (по умолчанию FieldPulseDemo!2026)")

if __name__ == "__main__":
    with SessionLocal() as db:
        seed(db)
