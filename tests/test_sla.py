from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from app.sla import deadline, working_minutes, apply_pause

CAL = {"timezone":"Europe/Moscow",
       "week":{str(i):[["09:00","18:00"]] for i in range(5)},
       "holidays":[]}

def test_friday_1750_30_working_minutes():
    friday = datetime(2026,11,13,17,50,tzinfo=ZoneInfo("Europe/Moscow"))
    result=deadline(friday,30,CAL)
    assert result.astimezone(ZoneInfo("Europe/Moscow")).isoformat() == "2026-11-16T09:20:00+03:00"

def test_weekend_and_holiday_skip():
    cal=dict(CAL,holidays=["2026-11-16"])
    friday=datetime(2026,11,13,17,50,tzinfo=ZoneInfo("Europe/Moscow"))
    result=deadline(friday,30,cal)
    assert result.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()=="2026-11-17"

def test_working_pause_extensions():
    zone=ZoneInfo("Europe/Moscow")
    start=datetime(2026,11,13,16,0,tzinfo=zone)
    end=datetime(2026,11,16,10,0,tzinfo=zone)
    original=datetime(2026,11,16,12,0,tzinfo=zone)
    assert working_minutes(start,end,CAL)==180
    assert apply_pause(original,start,end,CAL).astimezone(zone).hour==15

def test_aware_naive_utc():
    assert deadline(datetime(2026,11,13,14,50),30,CAL).tzinfo==timezone.utc
