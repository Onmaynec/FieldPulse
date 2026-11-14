"""Measure real PostgreSQL query plans and latency for OFFSET vs keyset.

Read-only by default. Optional synthetic generation runs in an uncommitted
transaction and is rolled back. Only run --generate on a dedicated demo database.
"""
import argparse
import json
import os
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.models import Ticket, User
from app.registry_seek import key_for, seek_condition
from app.ticket_queries import filtered_ticket_query, ticket_order


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    first = int(index)
    last = min(first + 1, len(values)-1)
    return round(values[first]+(values[last]-values[first])*(index-first), 2)


def plan(db, statement):
    compiled = statement.compile(dialect=db.bind.dialect,
                                 compile_kwargs={"literal_binds": True})
    row = db.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled))).scalar()
    return row[0] if isinstance(row, (list, tuple)) else json.loads(row)[0]


def flatten_plans(node):
    items = [{"node": node.get("Node Type"), "relation": node.get("Relation Name"),
              "index": node.get("Index Name"),
              "actual_rows": node.get("Actual Rows"),
              "shared_hit_blocks": node.get("Shared Hit Blocks"),
              "shared_read_blocks": node.get("Shared Read Blocks")}]
    for sub in node.get("Plans", []):
        items.extend(flatten_plans(sub))
    return items


def measure(db, label, statement, repeats):
    times = []
    example = None
    for _ in range(repeats):
        clock = time.perf_counter()
        result = plan(db, statement)
        times.append((time.perf_counter()-clock)*1000)
        if example is None:
            example = result
    return {
        "case": label, "samples": repeats,
        "client_p50_ms": percentile(times, .50),
        "client_p95_ms": percentile(times, .95),
        "client_max_ms": round(max(times), 2),
        "server_execution_ms": example.get("Execution Time"),
        "server_planning_ms": example.get("Planning Time"),
        "plan_nodes": flatten_plans(example["Plan"]),
    }


def add_rollback_only_rows(db, n):
    # PostgreSQL only. The rows remain uncommitted and invisible to API/worker;
    # they are discarded on Session.rollback() even on error.
    db.execute(text("""
        INSERT INTO tickets
          (id, org_id, site_id, asset_id, creator_id, priority, kind,
           description, status, version, policy_snapshot, reaction_deadline,
           resolution_deadline, created_at)
        SELECT md5('fp-v06-synthetic-' || gs.n::text),
               t.org_id, t.site_id, t.asset_id, t.creator_id,
               CASE WHEN gs.n % 4 = 0 THEN 'P1'
                    WHEN gs.n % 4 = 1 THEN 'P2'
                    WHEN gs.n % 4 = 2 THEN 'P3' ELSE 'P4' END,
               'bench', 'FieldPulse benchmark synthetic ' || gs.n::text,
               'new', 1, t.policy_snapshot,
               now() + gs.n * INTERVAL '1 minute',
               now() + gs.n * INTERVAL '2 minutes',
               now() - gs.n * INTERVAL '1 second'
        FROM (SELECT * FROM tickets ORDER BY created_at ASC LIMIT 1) AS t
        CROSS JOIN generate_series(1, :count) AS gs(n)
    """), {"count": n})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/fieldpulse-search-plan.json"))
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--expected-min", type=int, default=2000)
    parser.add_argument("--generate", type=int, default=0,
                        help="extra rows (0/50000/200000), transaction rollback only")
    parser.add_argument("--allow-demo-write", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 50 or args.expected_min < 0:
        parser.error("Invalid repeats or expected row count")
    if args.generate not in (0, 50000, 200000):
        parser.error("Synthetic generation: only 50000 or 200000")
    if args.generate and not args.allow_demo_write:
        parser.error("Explicit --allow-demo-write required; demo PostgreSQL only")
    with SessionLocal() as db:
        if db.bind.dialect.name != "postgresql":
            parser.error("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) requires PostgreSQL")
        if args.generate:
            add_rollback_only_rows(db, args.generate)
            db.execute(text("ANALYZE tickets"))
        total = db.scalar(select(func.count(Ticket.id))) or 0
        if total < args.expected_min:
            parser.error("Need at least %s tickets; found %s" % (args.expected_min, total))
        user = db.scalar(select(User).where(User.role == "dispatcher"))
        if user is None:
            parser.error("Seed dispatcher required")
        depth = max(0, (total // 2 // 20)*20)
        results = []
        for sort in ("newest", "deadline", "priority"):
            stmt = filtered_ticket_query(user)
            ordered = stmt.order_by(*ticket_order(sort))
            shallow = ordered.limit(20)
            deep = ordered.offset(depth).limit(20)
            # An extra SELECT identifies the cursor for the same deep page;
            # the benchmark excludes this one-off setup from query latency.
            anchor = db.scalars(ordered.offset(max(0, depth-1)).limit(1)).first()
            cursor_stmt = (stmt.where(seek_condition(key_for(anchor, sort), sort))
                           .order_by(*ticket_order(sort)).limit(20)) if depth else shallow
            results.extend([
                measure(db, sort + ":shallow", shallow, args.repeats),
                measure(db, sort + ":offset", deep, args.repeats),
                measure(db, sort + ":seek", cursor_stmt, args.repeats),
            ])
        text_query = filtered_ticket_query(user, q="benchmark").order_by(
            *ticket_order("newest")).limit(20)
        results.append(measure(db, "text:benchmark", text_query, args.repeats))
        version = db.scalar(text("SHOW server_version"))
        extensions = db.scalars(text("SELECT extname FROM pg_extension")).all()
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataset_rows": total, "generated_rows_uncommitted": args.generate,
            "deep_offset": depth,
            "environment": {"os": platform.platform(), "cpu_count": os.cpu_count(),
                            "postgres_version": version,
                            "pg_trgm": "pg_trgm" in extensions},
            "repetitions": args.repeats,
            "measurements": results,
            "limitations": (
                "Single process, demo synthetic/seed workload; client timing includes EXPLAIN. "
                "Generated rows are rolled back; results are not long-duration production SLOs."
            ),
        }
        # Even a failed write must not leave benchmarks in the database.
        db.rollback()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print("PostgreSQL plan benchmark: %s rows, %s cases → %s" % (
        total, len(results), args.output))


if __name__ == "__main__":
    main()
