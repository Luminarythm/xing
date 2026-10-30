"""Local preview/commit for validated JSONL samples; atomic whole-file imports."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from .samples import validate, MAX_BYTES
from .limits import CapacityExceeded, json_bytes, Limits
from .service import Service, Conflict
from .db import Database


def load_samples(path):
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_BYTES+1)
    result = validate(io.BytesIO(raw))
    if not result['valid']:
        raise ValueError('sample validation failed; run python -m geo.samples first')
    # Parse exactly the immutable bytes already validated, never reopen the file.
    grouped = {}
    for line in io.BytesIO(raw):
        row = json.loads(line)
        grouped.setdefault(row['source'].strip(), []).append(row['record'])
    batches = [dict(source=source, adapter='manual-v1', records=rows[i:i+500])
               for source, rows in grouped.items() for i in range(0, len(rows), 500)]
    return hashlib.sha256(raw).hexdigest(), batches, result['checked_rows']


class Imports:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def context(self, conn, site_id):
        config = self.service.brands.configuration(site_id)['id']
        watermark = conn.execute('SELECT before FROM retention_watermarks WHERE site_id=?', (site_id,)).fetchone()
        return dict(brand_config_id=config, retention_before=watermark[0] if watermark else None)

    def run_batches(self, conn, site_id, batches):
        results = []
        for index, body in enumerate(batches, 1):
            conn.execute('SAVEPOINT import_batch')
            try:
                counts = self.service.ingest(site_id, 'answers', body, connection=conn)
                results.append(dict(batch=index, source=body['source'], rows=len(body['records']), status='ready', **counts))
            except (Conflict, ValueError) as exc:
                conn.execute('ROLLBACK TO import_batch')
                results.append(dict(batch=index, source=body['source'], rows=len(body['records']),
                                    status='conflict' if isinstance(exc, Conflict) else 'rejected'))
            finally:
                conn.execute('RELEASE import_batch')
        return results

    def save(self, conn, plan):
        payload = json_bytes(plan, min(self.service.limits.query_bytes, self.service.limits.response_bytes)).decode()
        conn.execute('INSERT INTO sample_imports VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                     (plan['id'], plan['site_id'], payload))

    def read(self, conn, site_id, plan_id):
        row = conn.execute('SELECT length(CAST(payload AS BLOB)) FROM sample_imports WHERE site_id=? AND id=?', (site_id, plan_id)).fetchone()
        if row is None:
            raise LookupError('import preview not found')
        if row[0] > min(self.service.limits.query_bytes, self.service.limits.response_bytes):
            raise CapacityExceeded('import plan capacity exceeded')
        return json.loads(conn.execute('SELECT payload FROM sample_imports WHERE site_id=? AND id=?', (site_id, plan_id)).fetchone()[0])

    def preview(self, site_id, path):
        digest, batches, count = load_samples(path)
        if count > self.service.limits.query_events:
            raise CapacityExceeded('import row capacity exceeded')
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            context = self.context(conn, site_id)
            conn.execute('SAVEPOINT import_preview')
            results = self.run_batches(conn, site_id, batches)
            conn.execute('ROLLBACK TO import_preview')
            conn.execute('RELEASE import_preview')
            plan = dict(id=str(uuid.uuid4()), site_id=site_id, sha256=digest, rows=count, batches=results,
                        context=context, state='preview', executable=all(r['status'] == 'ready' for r in results),
                        expires=time.time()+900, attempts=0)
            self.save(conn, plan)
        return plan

    def execute(self, site_id, path, plan_id, confirm):
        if confirm != 'import-previewed-samples':
            raise ValueError('explicit import-previewed-samples confirmation required')
        digest, batches, count = load_samples(path)
        changed = False
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            plan = self.read(conn, site_id, plan_id)
            if digest != plan['sha256']:
                raise Conflict('input file changed; preview again')
            if plan['state'] == 'committed':
                return plan
            if not plan['executable'] or plan['expires'] < time.time():
                raise ValueError('preview blocked or expired; preview again')
            if count > self.service.limits.query_events:
                raise CapacityExceeded('import row capacity exceeded')
            if self.context(conn, site_id) != plan['context']:
                raise Conflict('brand configuration or retention boundary changed; preview again')
            conn.execute('SAVEPOINT import_commit')
            results = self.run_batches(conn, site_id, batches)
            plan['attempts'] += 1
            if results != plan['batches']:
                conn.execute('ROLLBACK TO import_commit')
                plan['last_attempt'] = dict(status='changed', batches=results)
                changed = True
            else:
                plan.update(state='committed', committed_at=time.time(), inserted=sum(r['inserted'] for r in results),
                            duplicates=sum(r['duplicates'] for r in results))
            conn.execute('RELEASE import_commit')
            self.save(conn, plan)
        if changed:
            raise Conflict('database selection changed; no samples imported; preview again')
        return plan

    def result(self, site_id, plan_id):
        with self.db.connect() as conn:
            return self.read(conn, site_id, plan_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preview', 'execute', 'result'])
    parser.add_argument('--db', required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--file')
    parser.add_argument('--plan')
    parser.add_argument('--confirm')
    args = parser.parse_args(argv)
    if args.action != 'result' and not args.file:
        parser.error('--file required')
    if args.action != 'preview' and not args.plan:
        parser.error('--plan required')
    try:
        if not Path(args.db).is_file():
            raise ValueError('existing database required')
        importer = Imports(Service(Database.from_env(args.db), Limits.from_env()))
        if args.action == 'preview':
            result = importer.preview(args.site, args.file)
        elif args.action == 'execute':
            result = importer.execute(args.site, args.file, args.plan, args.confirm)
        else:
            result = importer.result(args.site, args.plan)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, sqlite3.Error, ValueError, LookupError, CapacityExceeded):
        print(json.dumps(dict(error='import_operation_failed', action=args.action)))
        return 1


if __name__ == '__main__':
    sys.exit(main())
