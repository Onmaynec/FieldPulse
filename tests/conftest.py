import pytest
from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Asset, Contract, Organization, Shift, Site, SLAPolicy, User, now, uid
from app.security import hash_password

@pytest.fixture()
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        svc = Organization(id=uid(), name="service")
        a = Organization(id=uid(), name="A")
        b = Organization(id=uid(), name="B")
        db.add_all([svc,a,b])
        pwd = hash_password("test-pass")
        dispatcher = User(id=uid(), org_id=svc.id, role="dispatcher", name="Dispatcher",
                          email="dispatcher@test.local", password_hash=pwd)
        engineer = User(id=uid(), org_id=svc.id, role="engineer", name="Engineer",
                        email="engineer@test.local", password_hash=pwd, skills=["electrical"], area="north")
        wrong = User(id=uid(), org_id=svc.id, role="engineer", name="Wrong",
                     email="wrong@test.local", password_hash=pwd, skills=["hvac"], area="south")
        client_a = User(id=uid(), org_id=a.id, role="client", name="Client A",
                        email="a@test.local", password_hash=pwd)
        client_b = User(id=uid(), org_id=b.id, role="client", name="Client B",
                        email="b@test.local", password_hash=pwd)
        db.add_all([dispatcher,engineer,wrong,client_a,client_b])
        sa=Site(id=uid(),org_id=a.id,name="Site A",city="Moscow",area="north",timezone="Europe/Moscow")
        sb=Site(id=uid(),org_id=b.id,name="Site B",city="Moscow",area="south",timezone="Europe/Moscow")
        db.add_all([sa,sb])
        aa=Asset(id=uid(),site_id=sa.id,name="Panel",kind="electrical")
        ab=Asset(id=uid(),site_id=sb.id,name="Panel B",kind="electrical")
        db.add_all([aa,ab])
        policy=SLAPolicy(id=uid(),name="P1",priority="P1",reaction_minutes=30,
                         resolution_minutes=240,allowed_pauses=["awaiting_access"])
        db.add(policy)
        calendar={"timezone":"Europe/Moscow",
                  "week":{str(d):[["09:00","18:00"]] for d in range(5)},"holidays":[]}
        db.add_all([Contract(site_id=sa.id,policy_ids={"P1":policy.id},calendar=calendar),
                    Contract(site_id=sb.id,policy_ids={"P1":policy.id},calendar=calendar)])
        db.add(Shift(engineer_id=engineer.id,starts_at=now()-timedelta(days=1),
                     ends_at=now()+timedelta(days=1)))
        db.commit()
        ids={"a_site":sa.id,"a_asset":aa.id,"b_site":sb.id,"b_asset":ab.id,
             "engineer":engineer.id,"wrong":wrong.id,"policy":policy.id}
    def override():
        with Session() as session:yield session
    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        yield client,Session,ids
    app.dependency_overrides.clear()
    engine.dispose()

def signin(client, email):
    result=client.post("/api/v1/auth/login",json={"email":email,"password":"test-pass"})
    assert result.status_code==200,result.text
    return client.cookies["fp_csrf"]

def post(client,path,body,csrf,headers=None):
    return client.post("/api/v1"+path,json=body,headers={"X-CSRF-Token":csrf,**(headers or {})})

def create_ticket(client,ids,csrf,site="a"):
    return post(client,"/tickets",{"site_id":ids[site+"_site"],"asset_id":ids[site+"_asset"],
                "priority":"P1","kind":"repair","description":"Equipment failed during inspection"},csrf,
                {"Idempotency-Key":"test-create-"+site})
