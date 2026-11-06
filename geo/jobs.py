"""Local synthetic jobs only. Durable leases fence stale workers and cap retries."""
import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from .contracts import text, timestamp


class Jobs:
    def __init__(self, service, clock=time.time):
        self.service, self.db, self.clock = service, service.db, clock

    def enqueue(self, site_id, body):
        self.service.site(site_id)
        if body.get('provider', 'mock') != 'mock':
            raise ValueError('only the local mock provider is enabled')
        key = text(body.get('request_key'), 'request_key', 200)
        prompt = text(body.get('prompt'), 'prompt', 5000)
        observed = timestamp(body['observed_at']) if 'observed_at' in body else datetime.now(timezone.utc).isoformat()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM sample_jobs WHERE site_id=? AND request_key=?', (site_id, key)).fetchone()
            if old:
                if json.loads(old['record'])['prompt'] != prompt or ('observed_at' in body and old['observed_at'] != observed):
                    raise ValueError('request_key reused with different input')
                return self.public(old)
            watermark = conn.execute('SELECT before FROM retention_watermarks WHERE site_id=?', (site_id,)).fetchone()
            if watermark and observed < watermark[0]:
                raise ValueError('job predates retention window')
            job_id = str(uuid.uuid4())
            record = dict(event_id=job_id, observed_at=observed, platform='synthetic', model='mock-v1', surface='manual',
                          prompt=prompt, answer='Synthetic local fixture. No model was called.', citations=[])
            conn.execute('INSERT INTO sample_jobs(id,site_id,request_key,observed_at,record,state,next_at) VALUES(?,?,?,?,?,?,?)',
                         (job_id, site_id, key, observed, json.dumps(record), 'queued', self.clock()))
            return self.public(conn.execute('SELECT * FROM sample_jobs WHERE id=?', (job_id,)).fetchone())

    def public(self, row):
        return {k: row[k] for k in ('id', 'site_id', 'request_key', 'state', 'attempts', 'next_at', 'error')}

    def list(self, site_id):
        self.service.site(site_id)
        with self.db.connect() as conn:
            return [self.public(r) for r in conn.execute('SELECT * FROM sample_jobs WHERE site_id=? ORDER BY rowid DESC LIMIT 100', (site_id,))]

    def claim(self):
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE sample_jobs SET state='failed',error='attempt_limit' WHERE state='running' AND lease_until<=? AND attempts>=3", (self.clock(),))
            row = conn.execute("""SELECT * FROM sample_jobs WHERE attempts<3 AND
                ((state='queued' AND next_at<=?) OR (state='running' AND lease_until<=?)) ORDER BY rowid LIMIT 1""",
                (self.clock(), self.clock())).fetchone()
            if row is None:
                return None
            lease = str(uuid.uuid4())
            conn.execute("UPDATE sample_jobs SET state='running',attempts=attempts+1,lease=?,lease_until=? WHERE id=?", (lease, self.clock()+30, row['id']))
            return dict(row, lease=lease, attempts=row['attempts']+1)

    def finish(self, claim, fail=False):
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM sample_jobs WHERE id=? AND state='running' AND lease=? AND lease_until>?",
                               (claim['id'], claim['lease'], self.clock())).fetchone()
            if row is None:
                return False
            if fail:
                conn.execute('UPDATE sample_jobs SET state=?,next_at=?,error=?,lease=NULL WHERE id=?',
                    ('failed' if row['attempts'] >= 3 else 'queued', self.clock()+2**row['attempts'], 'mock_failure', row['id']))
                return False
            self.service.ingest(row['site_id'], 'answers', dict(adapter='manual-v1', source='mock-jobs', records=[json.loads(row['record'])]), connection=conn)
            conn.execute("UPDATE sample_jobs SET state='succeeded',error=NULL,lease=NULL WHERE id=?", (row['id'],))
            return True

    def run_one(self):
        claim = self.claim()
        try:
            success = self.finish(claim) if claim else False
        except (ValueError, LookupError):
            self.finish(claim, fail=True)
            success = False
        return dict(claimed=bool(claim), succeeded=success)


if __name__ == '__main__':
    from .db import Database
    from .service import Service
    parser = argparse.ArgumentParser(description='Run at most one persisted local synthetic sampling job.')
    parser.parse_args()
    print(json.dumps(Jobs(Service(Database.from_env(os.getenv('GEO_DB', 'data/geo.sqlite3')))).run_one()))
