"""Shared, server-authorized registry filters and deterministic ordering.

Every registry query starts from an authenticated role scope. User-supplied
sort keys never become SQL text; filtering uses SQLAlchemy bind parameters.
"""
from sqlalchemy import or_, select

from .models import Asset, Site, Ticket, User

OPEN = ("new", "triage", "assigned", "accepted", "in_progress", "approval", "returned")


def filtered_ticket_query(user, *, q="", status=None, priority=None, active=False):
    stmt = (select(Ticket)
            .join(Site, Site.id == Ticket.site_id)
            .join(Asset, Asset.id == Ticket.asset_id)
            .outerjoin(User, User.id == Ticket.assignee_id))
    if user.role == "client":
        stmt = stmt.where(Ticket.org_id == user.org_id)
    elif user.role == "engineer":
        stmt = stmt.where(Ticket.assignee_id == user.id)
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
