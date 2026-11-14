"""Shared, server-authorized registry filters and deterministic ordering.

Every registry query starts from an authenticated role scope. User-supplied
sort keys never become SQL text; filtering uses SQLAlchemy bind parameters.
"""
from sqlalchemy import false, or_, select, true

from .models import Asset, Site, Ticket, User

OPEN = ("new", "triage", "assigned", "accepted", "in_progress", "approval", "returned")
STAFF = ("admin", "dispatcher", "manager")


def visibility(user):
    if user.role == "client":
        return Ticket.org_id == user.org_id
    if user.role == "engineer":
        return Ticket.assignee_id == user.id
    if user.role in STAFF:
        return true()
    return false()



def filtered_ticket_query(user, *, q="", status=None, priority=None, active=False):
    stmt = (select(Ticket)
            .join(Site, Site.id == Ticket.site_id)
            .join(Asset, Asset.id == Ticket.asset_id)
            .outerjoin(User, User.id == Ticket.assignee_id))
    stmt = stmt.where(visibility(user))
    if status:
        stmt = stmt.where(Ticket.status == status)
    if priority:
        stmt = stmt.where(Ticket.priority == priority)
    if active:
        stmt = stmt.where(Ticket.status.in_(OPEN))
    term = q.strip()
    if term:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        match = "%" + escaped + "%"
        stmt = stmt.where(or_(
            Ticket.id.ilike(match, escape="\\"),
            Ticket.description.ilike(match, escape="\\"),
            Site.name.ilike(match, escape="\\"),
            Asset.name.ilike(match, escape="\\"),
            User.name.ilike(match, escape="\\"),
        ))
    return stmt


def ticket_order(sort):
    if sort == "priority":
        return (Ticket.priority.asc(), Ticket.resolution_deadline.asc(), Ticket.id.desc())
    if sort == "deadline":
        return (Ticket.resolution_deadline.asc(), Ticket.id.desc())
    return (Ticket.created_at.desc(), Ticket.id.desc())
