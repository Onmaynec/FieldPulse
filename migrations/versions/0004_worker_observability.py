"""Worker telemetry and query indexes.

Revision ID: 0004_worker_observability
Revises: 0003_ticket_views
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_worker_observability"
down_revision = "0003_ticket_views"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("worker_cycles"):
        op.create_table(
            "worker_cycles",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("last_started_at", sa.DateTime(timezone=True)),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.Column("last_failure_at", sa.DateTime(timezone=True)),
            sa.Column("last_duration_ms", sa.Integer()),
            sa.Column("last_emitted", sa.Integer(), nullable=False),
            sa.Column("cycles", sa.Integer(), nullable=False),
            sa.Column("failures", sa.Integer(), nullable=False),
            sa.Column("last_error_type", sa.String(80)),
        )
    existing = {x["name"] for x in sa.inspect(bind).get_indexes("tickets")}
    indexes = {
        "ix_ticket_reaction_due": ["status", "reaction_deadline"],
        "ix_ticket_deadline_seek": [sa.text("resolution_deadline ASC"), sa.text("id DESC")],
        "ix_ticket_priority_seek": [sa.text("priority ASC"), sa.text("resolution_deadline ASC"), sa.text("id DESC")],
    }
    for name, columns in indexes.items():
        if name not in existing:
            op.create_index(name, "tickets", columns)

    # pg_trgm is a trusted extension on supported PostgreSQL versions,
    # but managed deployments may still forbid CREATE EXTENSION.
    # Each optional operation is guarded by its own savepoint.
    if bind.dialect.name == "postgresql":
        try:
            with bind.begin_nested():
                bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        except (sa.exc.DBAPIError, sa.exc.SQLAlchemyError):
            pass
        available = bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='pg_trgm')"))
        if available:
            for table, column, index in (
                ("tickets", "description", "ix_ticket_description_trgm"),
                ("sites", "name", "ix_sites_name_trgm"),
                ("assets", "name", "ix_assets_name_trgm"),
                ("users", "name", "ix_users_name_trgm"),
            ):
                try:
                    with bind.begin_nested():
                        bind.execute(sa.text(
                            f"CREATE INDEX IF NOT EXISTS {index} ON {table} USING gin ({column} gin_trgm_ops)"
                        ))
                except (sa.exc.DBAPIError, sa.exc.SQLAlchemyError):
                    # Keep the upgrade usable on restricted clusters.
                    pass


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in ("ix_ticket_description_trgm", "ix_sites_name_trgm",
                     "ix_assets_name_trgm", "ix_users_name_trgm"):
            bind.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    existing = {x["name"] for x in sa.inspect(bind).get_indexes("tickets")}
    for name in ("ix_ticket_reaction_due", "ix_ticket_deadline_seek",
                 "ix_ticket_priority_seek"):
        if name in existing:
            op.drop_index(name, table_name="tickets")
    if sa.inspect(bind).has_table("worker_cycles"):
        op.drop_table("worker_cycles")
