"""Authenticated operational summary without prompts, credentials or filesystem paths."""
from dataclasses import asdict
from pathlib import Path
import sqlite3
import time


def runtime_status(service):
    # Read-only URI avoids creating a replacement database if the configured file vanishes.
    uri = Path(service.db.path).resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        deadline = time.monotonic() + 2
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.execute('BEGIN')
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version != 7:
            raise sqlite3.DatabaseError('unsupported schema')
        required = {'sites', 'events', 'inventories', 'brand_configs', 'mention_reviews', 'site_security',
                    'ingest_keys', 'event_positions', 'retention_plans', 'retention_watermarks', 'sample_jobs', 'site_positions', 'sample_imports'}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise sqlite3.DatabaseError('missing required tables')
        counts = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('sites', 'events', 'inventories', 'brand_configs', 'mention_reviews')}
        jobs = dict(conn.execute('SELECT state,COUNT(*) FROM sample_jobs GROUP BY state'))
        now = time.time()
        due = conn.execute("SELECT COUNT(*) FROM sample_jobs WHERE state='queued' AND next_at<=?", (now,)).fetchone()[0]
        expired = conn.execute("SELECT COUNT(*) FROM sample_jobs WHERE state='running' AND lease_until<=?", (now,)).fetchone()[0]
        return dict(status='ok', schema_version=version, counts=counts,
                    jobs=dict(by_state=jobs, due=due, expired_leases=expired),
                    limits=asdict(service.limits), providers=['mock'], bind_scope='loopback_by_default',
                    database_timeouts_ms=dict(busy=service.db.busy_timeout_ms, query=service.db.query_timeout_ms))
    finally:
        conn.close()
