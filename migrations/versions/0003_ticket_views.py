"""User-owned ticket views and search indexes.

Revision ID: 0003_ticket_views
Revises: 0002_worklog_hash
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_ticket_views"
down_revision = "0002_worklog_hash"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ticket_views"):
        op.create_table(
            "ticket_views",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("filters", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "name", name="uq_ticket_view_user_name"),
        )
        op.create_index("ix_ticket_views_user_id", "ticket_views", ["user_id"])
    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("tickets")}
    if "ix_ticket_org_created_id" not in indexes:
        op.create_index("ix_ticket_org_created_id", "tickets", ["org_id", "created_at", "id"])
    if "ix_ticket_assignee_created_id" not in indexes:
        op.create_index("ix_ticket_assignee_created_id", "tickets", ["assignee_id", "created_at", "id"])
    if "ix_ticket_status_deadline" not in indexes:
        op.create_index("ix_ticket_status_deadline", "tickets", ["status", "resolution_deadline"])

def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for index in ("ix_ticket_status_deadline", "ix_ticket_assignee_created_id", "ix_ticket_org_created_id"):
        if index in {idx["name"] for idx in sa.inspect(bind).get_indexes("tickets")}:
            op.drop_index(index, table_name="tickets")
    if inspector.has_table("ticket_views"):
        op.drop_table("ticket_views")
