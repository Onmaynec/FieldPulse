"""Inject one harmless CI-only operational incident into a disposable test DB."""
from app.db import SessionLocal
from app.models import OperationalIncident, now


def main():
    with SessionLocal() as db:
        item = OperationalIncident(source="browser-regression", state="error",
                                   opened_at=now(), last_seen_at=now())
        db.add(item)
        db.commit()
        print("CI operational incident seeded: " + item.id)


if __name__ == "__main__":
    main()
