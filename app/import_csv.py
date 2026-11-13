"""CSV import for organizations, sites, equipment and SLA contracts.

Import order: sites, assets, contracts. Validation errors roll back the entire file.
Exported IDs are internal; input uses stable name+city keys.
"""
import argparse
import csv
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from .db import SessionLocal
from .models import Asset, Contract, Organization, Site, SLAPolicy

KINDS = {"electrical", "hvac", "low_voltage"}
PRIORITIES = {"P1", "P2", "P3", "P4"}
PAUSES = {"awaiting_access", "awaiting_parts"}

def rows(filename):
    with open(filename, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV must have a header")
        for n, row in enumerate(reader, 2):
            yield n, {k.strip(): (v or "").strip() for k, v in row.items() if k is not None}

def required(row, names, lineno):
    missing = [name for name in names if not row.get(name)]
    if missing:
        raise ValueError("Line %d: missing columns: %s" % (lineno, ", ".join(missing)))

def site_lookup(db, row, lineno):
    required(row, ["site_name", "city"], lineno)
    matches = db.scalars(select(Site).where(
        Site.name == row["site_name"], Site.city == row["city"])).all()
    if len(matches) != 1:
        raise ValueError("Line %d: site not found or ambiguous: %s, %s" % (
            lineno, row["site_name"], row["city"]))
    return matches[0]

def import_sites(db, filename):
    count = 0
    for lineno, row in rows(filename):
        required(row, ["organization", "site_name", "city", "area", "timezone"], lineno)
        try:
            ZoneInfo(row["timezone"])
        except ZoneInfoNotFoundError:
            raise ValueError("Line %d: invalid timezone" % lineno)
        org = db.scalar(select(Organization).where(Organization.name == row["organization"]))
        if org is None:
            org = Organization(name=row["organization"])
            db.add(org)
            db.flush()
        existing = db.scalar(select(Site).where(
            Site.org_id == org.id, Site.name == row["site_name"], Site.city == row["city"]))
        if existing:
            continue
        db.add(Site(org_id=org.id, name=row["site_name"], city=row["city"],
                    area=row["area"], timezone=row["timezone"]))
        count += 1
    return count

def import_assets(db, filename):
    count = 0
    for lineno, row in rows(filename):
        required(row, ["site_name", "city", "asset_name", "kind"], lineno)
        site = site_lookup(db, row, lineno)
        if row["kind"] not in KINDS:
            raise ValueError("Line %d: unsupported equipment kind" % lineno)
        existing = db.scalar(select(Asset).where(
            Asset.site_id == site.id, Asset.name == row["asset_name"]))
        if existing:
            continue
        db.add(Asset(site_id=site.id, name=row["asset_name"], kind=row["kind"]))
        count += 1
    return count

def import_contracts(db, filename):
    count = 0
    for lineno, row in rows(filename):
        required(row, ["site_name", "city", "priority", "reaction_minutes",
                       "resolution_minutes", "working_days", "working_start",
                       "working_end"], lineno)
        site = site_lookup(db, row, lineno)
        priority = row["priority"].upper()
        if priority not in PRIORITIES:
            raise ValueError("Line %d: priority must be P1-P4" % lineno)
        try:
            reaction = int(row["reaction_minutes"])
            resolution = int(row["resolution_minutes"])
            days = [int(x.strip()) for x in row["working_days"].split(";")]
            if any(x not in range(7) for x in days) or not days or reaction < 1 or resolution < 1:
                raise ValueError()
            start, end = row["working_start"], row["working_end"]
            from .sla import parse_hhmm
            if parse_hhmm(start) >= parse_hhmm(end):
                raise ValueError()
            pauses = [x.strip() for x in row.get("allowed_pauses", "").split(";") if x.strip()]
            if not set(pauses).issubset(PAUSES):
                raise ValueError()
        except ValueError:
            raise ValueError("Line %d: invalid SLA minutes, time or pause" % lineno)
        calendar = {
            "timezone": site.timezone, "week": {
                str(day): [[start, end]] for day in days},
            "holidays": [x.strip() for x in row.get("holidays", "").split(";") if x.strip()]
        }
        contract = db.scalar(select(Contract).where(Contract.site_id == site.id))
        if contract and contract.calendar != calendar:
            raise ValueError("Line %d: conflicting calendar for site; group policies by same schedule" % lineno)
        if contract is None:
            contract = Contract(site_id=site.id, calendar=calendar, policy_ids={})
            db.add(contract)
            db.flush()
        mapping = dict(contract.policy_ids)
        if priority in mapping:
            existing = db.get(SLAPolicy, mapping[priority])
            if (existing.reaction_minutes == reaction and
                existing.resolution_minutes == resolution and
                existing.allowed_pauses == pauses):
                continue
        policy = SLAPolicy(name="%s / %s" % (site.name, priority),
                           priority=priority, reaction_minutes=reaction,
                           resolution_minutes=resolution, allowed_pauses=pauses)
        db.add(policy)
        db.flush()
        mapping[priority] = policy.id
        contract.policy_ids = mapping
        count += 1
    return count

def main():
    parser = argparse.ArgumentParser(description="FieldPulse CSV import")
    parser.add_argument("kind", choices=("sites", "assets", "contracts"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="validate entire file without committing changes")
    args = parser.parse_args()
    with SessionLocal() as db:
        try:
            importer = {"sites": import_sites, "assets": import_assets,
                        "contracts": import_contracts}[args.kind]
            count = importer(db, args.file)
            if args.dry_run:
                db.rollback()
                print("Validated %s (%d new records), no changes saved" % (args.file, count))
            else:
                db.commit()
                print("Imported %d records from %s" % (count, args.file))
        except Exception as exc:
            db.rollback()
            print("Import failed: %s" % exc, file=sys.stderr)
            raise SystemExit(1)

if __name__ == "__main__":
    main()
