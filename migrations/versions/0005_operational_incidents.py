"""Durable SLA watchdog incidents and webhook retry queue.

Revision ID: 0005_operational_incidents
Revises: 0004_worker_observability
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_operational_incidents"
down_revision = "0004_worker_observability"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operational_incidents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recovered_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by_id", sa.String(32), sa.ForeignKey("users.id")),
    )
    op.create_index("ix_operational_incidents_source", "operational_incidents", ["source"])
    op.create_index("uq_operational_incident_open_source", "operational_incidents",
                    ["source"], unique=True,
                    postgresql_where=sa.text("recovered_at IS NULL"),
                    sqlite_where=sa.text("recovered_at IS NULL"))
    op.create_table(
        "incident_deliveries",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("incident_id", sa.String(32),
                  sa.ForeignKey("operational_incidents.id"), nullable=False),
        sa.Column("event", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_type", sa.String(80)),
        sa.UniqueConstraint("incident_id", "event", name="uq_incident_delivery_event"),
    )
    op.create_index("ix_incident_deliveries_incident_id", "incident_deliveries",
                    ["incident_id"])
    op.create_index("ix_incident_deliveries_next_attempt_at", "incident_deliveries",
                    ["next_attempt_at"])
    op.create_table(
        "monitor_cycles",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("cycles", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("monitor_cycles")
    op.drop_table("incident_deliveries")
    op.drop_table("operational_incidents")
