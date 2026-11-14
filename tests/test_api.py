import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Act, Attachment, Event, SLAPolicy, Ticket, Worklog, now
from app.worker import run_once
from conftest import signin, post, create_ticket

def setup_assigned(env):
    client, Session, ids=env
    ca=signin(client,"a@test.local")
    response=create_ticket(client,ids,ca)
    assert response.status_code==201,response.text
    t=response.json()
    cd=signin(client,"dispatcher@test.local")
    response=post(client,"/tickets/"+t["id"]+"/assign",
                  {"version":t["version"],"engineer_id":ids["engineer"]},cd)
    assert response.status_code==200,response.text
    return response.json()

def test_cross_tenant_client_cannot_read_ticket_or_act(env):
    client,_,ids=env
    ca=signin(client,"a@test.local")
    t=create_ticket(client,ids,ca).json()
    signin(client,"b@test.local")
    assert client.get("/api/v1/tickets/"+t["id"]).status_code==404
    assert client.get("/api/v1/tickets/"+t["id"]+"/timeline").status_code==404
    assert client.get("/api/v1/assets?site_id="+ids["a_site"]).status_code==404

def test_csrf_required(env):
    client,_,ids=env
    signin(client,"a@test.local")
    response=client.post("/api/v1/tickets",json={
        "site_id":ids["a_site"],"asset_id":ids["a_asset"],"priority":"P1",
        "kind":"repair","description":"Something is not working"},
        headers={"Idempotency-Key":"csrf-test"})
    assert response.status_code==403

def test_create_idempotency(env):
    client,Session,ids=env
    csrf=signin(client,"a@test.local")
    first=create_ticket(client,ids,csrf)
    second=create_ticket(client,ids,csrf)
    assert first.status_code==201 and second.status_code==201
    assert first.json()["id"]==second.json()["id"]
    with Session() as db:
        assert db.scalar(select(func.count(Ticket.id)))==1

def test_eligible_assignment_is_rechecked(env):
    client,_,ids=env
    ca=signin(client,"a@test.local")
    t=create_ticket(client,ids,ca).json()
    cd=signin(client,"dispatcher@test.local")
    bad=post(client,"/tickets/"+t["id"]+"/assign",
             {"version":t["version"],"engineer_id":ids["wrong"]},cd)
    assert bad.status_code==422
    good=post(client,"/tickets/"+t["id"]+"/assign",
              {"version":t["version"],"engineer_id":ids["engineer"]},cd)
    assert good.status_code==200
    retry=post(client,"/tickets/"+t["id"]+"/assign",
               {"version":t["version"],"engineer_id":ids["engineer"]},cd)
    assert retry.status_code==409

def test_offline_sync_is_idempotent_and_cancel_conflicts(env):
    client,Session,ids=env
    t=setup_assigned(env)
    ce=signin(client,"engineer@test.local")
    r=post(client,"/tickets/"+t["id"]+"/accept",{"version":t["version"]},ce)
    assert r.status_code==200,r.text
    t=r.json()
    action={"uuid":str(uuid.uuid4()),"ticket_id":t["id"],"base_version":t["version"],
            "client_at":now().isoformat(),"summary":"Inspected the panel",
            "checklist":{"isolation":True},"materials":[{"name":"wire","quantity":2}],"photos":[]}
    first=post(client,"/mobile/sync",{"actions":[action]},ce)
    assert first.status_code==200,first.text
    assert first.json()["results"][0]["status"]=="applied"
    second=post(client,"/mobile/sync",{"actions":[action]},ce)
    assert second.json()["results"][0]["status"]=="duplicate"
    with Session() as db:
        assert db.scalar(select(func.count(Worklog.id)))==1
    cd=signin(client,"dispatcher@test.local")
    current=client.get("/api/v1/tickets/"+t["id"]).json()
    assert post(client,"/tickets/"+t["id"]+"/cancel",{"version":current["version"]},cd).status_code==200
    ce=signin(client,"engineer@test.local")
    action["uuid"]=str(uuid.uuid4())
    conflict=post(client,"/mobile/sync",{"actions":[action]},ce)
    assert conflict.json()["results"][0]["status"]=="conflict"
    with Session() as db:
        assert db.scalar(select(func.count(Worklog.id)))==1

def test_incomplete_checklist_rejected_by_server(env):
    client,_,_=env
    t=setup_assigned(env)
    ce=signin(client,"engineer@test.local")
    t=post(client,"/tickets/"+t["id"]+"/accept",{"version":t["version"]},ce).json()
    t=post(client,"/tickets/"+t["id"]+"/start",{"version":t["version"]},ce).json()
    r=post(client,"/tickets/"+t["id"]+"/submit",{"version":t["version"]},ce)
    assert r.status_code==422
    assert r.json()["details"]["code"]=="CHECKLIST_INCOMPLETE"
    assert set(r.json()["details"]["missing"])=={"isolation","voltage","result"}

def test_rejected_act_creates_next_revision(env):
    client,Session,ids=env
    t=setup_assigned(env)
    ce=signin(client,"engineer@test.local")
    t=post(client,"/tickets/"+t["id"]+"/accept",{"version":t["version"]},ce).json()
    t=post(client,"/tickets/"+t["id"]+"/start",{"version":t["version"]},ce).json()
    action={"uuid":str(uuid.uuid4()),"ticket_id":t["id"],"base_version":t["version"],
            "client_at":now().isoformat(),"summary":"Replaced faulty part",
            "checklist":{"isolation":True,"voltage":True,"result":True},"materials":[],"photos":[]}
    r=post(client,"/mobile/sync",{"actions":[action]},ce)
    assert r.json()["results"][0]["status"]=="applied"
    with Session() as db:
        db.add(Attachment(ticket_id=t["id"],uploader_id=ids["engineer"],
                          storage_key="test/photo.jpg",thumb_key="test/thumb.jpg",
                          checksum="1"*64,mime="image/jpeg",size=10))
        db.commit()
    t=client.get("/api/v1/tickets/"+t["id"]).json()
    submit=post(client,"/tickets/"+t["id"]+"/submit",{"version":t["version"]},ce)
    assert submit.status_code==200,submit.text
    act=submit.json()
    ca=signin(client,"a@test.local")
    decision=post(client,"/acts/"+act["id"]+"/decision",
                  {"version":act["version"],"decision":"rejected","reason":"Повторить проверку"},ca)
    assert decision.status_code==200,decision.text
    items=client.get("/api/v1/acts").json()
    assert {(a["version"],a["status"]) for a in items}=={(1,"rejected"),(2,"draft")}

def test_worker_deduplicates_after_restart_and_policy_edit(env):
    client,Session,ids=env
    ca=signin(client,"a@test.local")
    t=create_ticket(client,ids,ca).json()
    with Session() as db:
        ticket=db.get(Ticket,t["id"])
        ticket.reaction_deadline=now()-timedelta(minutes=60)
        ticket.resolution_deadline=now()-timedelta(minutes=60)
        db.commit()
    with Session() as db:
        assert run_once(db)==2
        assert run_once(db)==0
    with Session() as db:
        policy=db.get(SLAPolicy,ids["policy"])
        policy.reaction_minutes=9999
        db.commit()
        assert run_once(db)==0
        assert db.scalar(select(func.count(Event.id)).where(Event.type.like("sla_%_breached")))==2

def test_percentile_single_value():
    from app.api import percentile
    assert percentile([17.25], 0.95)==17.25

def test_foreign_tenant_cannot_open_act(env):
    client,Session,ids=env
    ca=signin(client,"a@test.local")
    t=create_ticket(client,ids,ca).json()
    with Session() as db:
        item=Act(ticket_id=t["id"],version=1,status="pending",content={"work":[]})
        db.add(item)
        db.commit()
        act_id=item.id
    signin(client,"b@test.local")
    assert client.get("/api/v1/acts/"+act_id).status_code==404
