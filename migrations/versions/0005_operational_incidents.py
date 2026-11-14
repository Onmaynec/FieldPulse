"""Durable SLA watchdog incidents and webhook retry queue.

Revision ID: 0005_operational_incidents
Revises: 0004_worker_observability

The original 0001 revision uses Base.metadata.create_all. On a fresh database
it may have already created these tables from current model metadata, so each
operation is conditional for both fresh installs and upgrades.
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_operational_incidents"
down_revision = "0004_worker_observability"
branch_labels = None
depends_on = None


def has_table(name):
    return sa.inspect(op.get_bind()).has_table(name)


def has_index(table, name):
    return name in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade():
    if not has_table("operational_incidents"):
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
    if not has_index("operational_incidents", "ix_operational_incidents_source"):
        op.create_index("ix_operational_incidents_source", "operational_incidents", ["source"])
    if not has_index("operational_incidents", "uq_operational_incident_open_source"):
        op.create_index("uq_operational_incident_open_source", "operational_incidents",
                        ["source"], unique=True,
                        postgresql_where=sa.text("recovered_at IS NULL"),
                        sqlite_where=sa.text("recovered_at IS NULL"))
    if not has_table("incident_deliveries"):
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
    if not has_index("incident_deliveries", "ix_incident_deliveries_incident_id"):
        op.create_index("ix_incident_deliveries_incident_id", "incident_deliveries",
                        ["incident_id"])
    if not has_index("incident_deliveries", "ix_incident_deliveries_next_attempt_at"):
        op.create_index("ix_incident_deliveries_next_attempt_at", "incident_deliveries",
                        ["next_attempt_at"])
    if not has_index("tickets", "ix_ticket_created_seek"):
        # Supports newest DESC/DESC via a reverse scan.
        op.create_index("ix_ticket_created_seek", "tickets",
                        ["created_at", "id"])
    if not has_table("monitor_cycles"):
        op.create_table(
            "monitor_cycles",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.Column("cycles", sa.Integer(), nullable=False),
        )


def downgrade():
    if has_index("tickets", "ix_ticket_created_seek"):
        op.drop_index("ix_ticket_created_seek", table_name="tickets")
    for table in ("monitor_cycles", "incident_deliveries", "operational_incidents"):
        if has_table(table):
            op.drop_table(table)
