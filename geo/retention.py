"""Preview-bound, site-scoped deletion with a monotonic ingestion watermark."""
import hashlib
import json
import time
import uuid
from .contracts import timestamp
from .service import Conflict
from .limits import CapacityExceeded, json_bytes


class SelectionBudget:
    """One combined budget for event keys, job IDs and review sequence numbers."""
    def __init__(self, limits):
        self.limits, self.count, self.size = limits, 0, 0

    def read(self, cursor):
        rows = []
        for row in cursor:
            self.count += 1
            self.size += len(json.dumps(list(row), ensure_ascii=False).encode('utf-8'))
            if self.count > self.limits.query_events or self.size > self.limits.query_bytes:
                raise CapacityExceeded('retention selection capacity exceeded; choose an earlier cutoff or increase the configured budget')
            rows.append(row)
        return rows


class Retention:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def selection(self, conn, site_id, before, high, budget):
        return budget.read(conn.execute('''SELECT p.sequence,e.kind,e.source,e.event_id FROM event_positions p JOIN events e
            ON (e.site_id,e.kind,e.source,e.event_id)=(p.site_id,p.kind,p.source,p.event_id)
            WHERE e.site_id=? AND e.observed_at<? AND p.sequence<=? ORDER BY p.sequence''', (site_id, before, high)))

    def digest(self, rows):
        return hashlib.sha256(json.dumps([list(r) for r in rows]).encode()).hexdigest()

    def related(self, conn, site_id, before, budget):
        jobs = [r[0] for r in budget.read(conn.execute('SELECT id FROM sample_jobs WHERE site_id=? AND observed_at<? ORDER BY id', (site_id, before)))]
        reviews = [r[0] for r in budget.read(conn.execute("""SELECT r.sequence FROM mention_reviews r WHERE r.site_id=? AND EXISTS
            (SELECT 1 FROM events e WHERE e.site_id=r.site_id AND e.kind='answers' AND e.observed_at<?
             AND e.source=json_extract(r.payload,'$.source') AND e.event_id=json_extract(r.payload,'$.event_id')) ORDER BY r.sequence""", (site_id, before)))]
        return dict(job_ids=jobs, review_sequences=reviews)

    def plan_json(self, plan):
        return json_bytes(plan, min(self.service.limits.query_bytes, self.service.limits.response_bytes)).decode('utf-8')

    def preview(self, site_id, before):
        self.service.site(site_id)
        before = timestamp(before)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            high = conn.execute('SELECT COALESCE(MAX(sequence),0) FROM event_positions').fetchone()[0]
            budget = SelectionBudget(self.service.limits)
            rows = self.selection(conn, site_id, before, high, budget)
            plan = dict(id=str(uuid.uuid4()), site_id=site_id, before=before, high=high, count=len(rows),
                        digest=self.digest(rows), expires=time.time()+900, state='preview')
            plan.update(self.related(conn, site_id, before, budget))
            plan.update(selected_entities=budget.count, selection_bytes=budget.size)
            serialized = self.plan_json(plan)
            conn.execute('INSERT INTO retention_plans VALUES(?,?,?)', (plan['id'], site_id, serialized))
        return plan

    def execute(self, site_id, plan_id, confirm):
        if confirm != 'delete-previewed-events':
            raise ValueError('explicit delete-previewed-events confirmation required')
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT length(CAST(payload AS BLOB)) FROM retention_plans WHERE site_id=? AND id=?', (site_id, plan_id)).fetchone()
            if row is None:
                raise LookupError('retention preview not found')
            if row[0] > min(self.service.limits.query_bytes, self.service.limits.response_bytes):
                raise CapacityExceeded('retention plan capacity exceeded; increase the configured budget')
            plan = json.loads(conn.execute('SELECT payload FROM retention_plans WHERE site_id=? AND id=?', (site_id, plan_id)).fetchone()[0])
            if plan['state'] == 'deleted':
                return plan
            if plan['expires'] < time.time():
                raise ValueError('retention preview expired')
            # Reject *any* new matching arrival so the confirmed preview stays exact.
            budget = SelectionBudget(self.service.limits)
            rows = self.selection(conn, site_id, plan['before'], 9223372036854775807, budget)
            if self.digest(rows) != plan['digest']:
                raise Conflict('retention selection changed; preview again')
            if self.related(conn, site_id, plan['before'], budget) != {k: plan[k] for k in ('job_ids', 'review_sequences')}:
                raise Conflict('related jobs or reviews changed; preview again')
            plan.update(state='deleted', deleted_at=time.time(), removed_jobs=len(plan['job_ids']))
            serialized = self.plan_json(plan)  # Validate the final response BEFORE any deletion.
            for _, kind, source, event_id in rows:
                if kind == 'answers':
                    conn.execute("DELETE FROM mention_reviews WHERE site_id=? AND json_extract(payload,'$.source')=? AND json_extract(payload,'$.event_id')=?", (site_id, source, event_id))
                conn.execute('DELETE FROM events WHERE site_id=? AND kind=? AND source=? AND event_id=?', (site_id, kind, source, event_id))
            # Job records may contain prompt/answer material: prune them too.
            conn.execute('DELETE FROM sample_jobs WHERE site_id=? AND observed_at<?', (site_id, plan['before']))
            conn.execute('''INSERT INTO retention_watermarks VALUES(?,?) ON CONFLICT(site_id)
                DO UPDATE SET before=MAX(before,excluded.before)''', (site_id, plan['before']))
            conn.execute('UPDATE retention_plans SET payload=? WHERE id=?', (serialized, plan_id))
        return plan
