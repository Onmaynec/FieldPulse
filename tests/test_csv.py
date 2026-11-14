import io
from sqlalchemy import select
from app.import_csv import import_sites, import_assets, import_contracts
from app.models import Site, Asset, Contract

def test_csv_import_and_repeat(env, tmp_path):
    _, Session, _ = env
    sites=tmp_path/"sites.csv"
    sites.write_text("organization,site_name,city,area,timezone\nClient X,New Tower,Moscow,north,Europe/Moscow\n",encoding="utf8")
    assets=tmp_path/"assets.csv"
    assets.write_text("site_name,city,asset_name,kind\nNew Tower,Moscow,Panel A,electrical\n",encoding="utf8")
    contracts=tmp_path/"contracts.csv"
    contracts.write_text("site_name,city,priority,reaction_minutes,resolution_minutes,working_days,working_start,working_end,allowed_pauses\nNew Tower,Moscow,P1,30,240,0;1;2;3;4,09:00,18:00,awaiting_access\n",encoding="utf8")
    with Session() as db:
        assert import_sites(db,sites)==1
        db.commit()
        assert import_sites(db,sites)==0
        assert import_assets(db,assets)==1
        db.commit()
        assert import_assets(db,assets)==0
        assert import_contracts(db,contracts)==1
        db.commit()
        assert import_contracts(db,contracts)==0
        site=db.scalar(select(Site).where(Site.name=="New Tower"))
        assert site is not None
        assert db.scalar(select(Contract).where(Contract.site_id==site.id)) is not None
