"""Offline request fingerprints and administrative audit.

Revision ID: 0002_worklog_hash
Revises: 0001_initial
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_worklog_hash"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = {c["name"] for c in inspector.get_columns("worklogs")}
    if "request_hash" not in names:
        op.add_column("worklogs", sa.Column("request_hash", sa.String(64), nullable=True))
    if not inspector.has_table("admin_audit"):
        op.create_table(
            "admin_audit",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("actor_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("action", sa.String(80), nullable=False),
            sa.Column("target", sa.String(160), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)
        )

def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("admin_audit"):
        op.drop_table("admin_audit")
    names = {c["name"] for c in inspector.get_columns("worklogs")}
    if "request_hash" in names:
        op.drop_column("worklogs", "request_hash")
