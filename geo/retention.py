"""Preview-bound, site-scoped deletion with a monotonic ingestion watermark."""
import hashlib
import json
import time
import uuid
from .contracts import timestamp
from .service import Conflict


class Retention:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def selection(self, conn, site_id, before, high):
        return conn.execute('''SELECT p.sequence,e.kind,e.source,e.event_id FROM event_positions p JOIN events e
            ON (e.site_id,e.kind,e.source,e.event_id)=(p.site_id,p.kind,p.source,p.event_id)
            WHERE e.site_id=? AND e.observed_at<? AND p.sequence<=? ORDER BY p.sequence''', (site_id, before, high)).fetchall()

    def digest(self, rows):
        return hashlib.sha256(json.dumps([list(r) for r in rows]).encode()).hexdigest()

    def related(self, conn, site_id, before):
        jobs = [r[0] for r in conn.execute('SELECT id FROM sample_jobs WHERE site_id=? AND observed_at<? ORDER BY id', (site_id, before))]
        reviews = [r[0] for r in conn.execute("""SELECT r.sequence FROM mention_reviews r WHERE r.site_id=? AND EXISTS
            (SELECT 1 FROM events e WHERE e.site_id=r.site_id AND e.kind='answers' AND e.observed_at<?
             AND e.source=json_extract(r.payload,'$.source') AND e.event_id=json_extract(r.payload,'$.event_id')) ORDER BY r.sequence""", (site_id, before))]
        return dict(job_ids=jobs, review_sequences=reviews)

    def preview(self, site_id, before):
        self.service.site(site_id)
        before = timestamp(before)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            high = conn.execute('SELECT COALESCE(MAX(sequence),0) FROM event_positions').fetchone()[0]
            rows = self.selection(conn, site_id, before, high)
            plan = dict(id=str(uuid.uuid4()), site_id=site_id, before=before, high=high, count=len(rows),
                        digest=self.digest(rows), expires=time.time()+900, state='preview')
            plan.update(self.related(conn, site_id, before))
            conn.execute('INSERT INTO retention_plans VALUES(?,?,?)', (plan['id'], site_id, json.dumps(plan)))
        return plan

    def execute(self, site_id, plan_id, confirm):
        if confirm != 'delete-previewed-events':
            raise ValueError('explicit delete-previewed-events confirmation required')
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT payload FROM retention_plans WHERE site_id=? AND id=?', (site_id, plan_id)).fetchone()
            if row is None:
                raise LookupError('retention preview not found')
            plan = json.loads(row[0])
            if plan['state'] == 'deleted':
                return plan
            if plan['expires'] < time.time():
                raise ValueError('retention preview expired')
            # Reject *any* new matching arrival so the confirmed preview stays exact.
            rows = self.selection(conn, site_id, plan['before'], 9223372036854775807)
            if self.digest(rows) != plan['digest']:
                raise Conflict('retention selection changed; preview again')
            if self.related(conn, site_id, plan['before']) != {k: plan[k] for k in ('job_ids', 'review_sequences')}:
                raise Conflict('related jobs or reviews changed; preview again')
            for _, kind, source, event_id in rows:
                if kind == 'answers':
                    conn.execute("DELETE FROM mention_reviews WHERE site_id=? AND json_extract(payload,'$.source')=? AND json_extract(payload,'$.event_id')=?", (site_id, source, event_id))
                conn.execute('DELETE FROM events WHERE site_id=? AND kind=? AND source=? AND event_id=?', (site_id, kind, source, event_id))
            # Job records may contain prompt/answer material: prune them too.
            removed_jobs = conn.execute('DELETE FROM sample_jobs WHERE site_id=? AND observed_at<?', (site_id, plan['before'])).rowcount
            conn.execute('''INSERT INTO retention_watermarks VALUES(?,?) ON CONFLICT(site_id)
                DO UPDATE SET before=MAX(before,excluded.before)''', (site_id, plan['before']))
            plan.update(state='deleted', deleted_at=time.time(), removed_jobs=removed_jobs)
            conn.execute('UPDATE retention_plans SET payload=? WHERE id=?', (json.dumps(plan), plan_id))
        return plan
