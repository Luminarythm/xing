"""Local import metadata paging and confirmed cleanup of expired previews only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from .cursors import encode, decode
from .db import Database
from .service import Service, Conflict
from .limits import Limits, CapacityExceeded, read_rows, json_bytes


class History:
    def __init__(self, service, secret):
        if not isinstance(secret, str) or len(secret) < 24:
            raise ValueError('a signing key of at least 24 characters is required')
        self.service, self.db, self.secret = service, service.db, secret

    def output(self, value):
        json_bytes(value, self.service.limits.response_bytes)
        return value

    def limit(self, value):
        if type(value) is not int or not 1 <= value <= 500:
            raise ValueError('limit must be 1..500')

    def page(self, site_id, limit=100, cursor=None):
        self.service.site(site_id)
        self.limit(limit)
        scope = dict(resource='import-history', site=site_id)
        with self.db.connect() as conn:
            if cursor:
                state = decode(cursor, self.secret, scope)
            else:
                high = conn.execute('SELECT COALESCE(MAX(sequence),0) FROM import_positions').fetchone()[0]
                state = dict(scope=scope, after=0, high=high, expires=time.time()+3600)
            rows = read_rows(conn.execute('''SELECT p.sequence,json_object(
                'id',i.id,'state',json_extract(i.payload,'$.state'),'rows',json_extract(i.payload,'$.rows'),
                'expires',json_extract(i.payload,'$.expires'),'committed_at',json_extract(i.payload,'$.committed_at'),
                'inserted',json_extract(i.payload,'$.inserted'),'duplicates',json_extract(i.payload,'$.duplicates'),
                'quality',json_extract(i.payload,'$.quality')) AS payload
                FROM import_positions p JOIN sample_imports i ON i.id=p.import_id
                WHERE i.site_id=? AND p.sequence>? AND p.sequence<=? ORDER BY p.sequence LIMIT ?''',
                (site_id, state['after'], state['high'], limit+1)), limit+1, self.service.limits.query_bytes)
        more, rows = len(rows) > limit, rows[:limit]
        next_cursor = None
        if more:
            state['after'] = rows[-1]['sequence']
            next_cursor = encode(state, self.secret)
        return self.output(dict(items=[dict(json.loads(r['payload']), sequence=r['sequence']) for r in rows],
                                next_cursor=next_cursor, snapshot_high_watermark=state['high'], limit=limit))

    def selection(self, conn, site_id, cutoff, high, limit):
        # Only bounded identifiers are read; no source names or sample bodies are exposed.
        return read_rows(conn.execute('''SELECT i.id,json_object('id',i.id,'expires',json_extract(i.payload,'$.expires')) AS payload
            FROM import_positions p JOIN sample_imports i ON i.id=p.import_id
            WHERE i.site_id=? AND p.sequence<=? AND json_extract(i.payload,'$.state')='preview'
            AND json_extract(i.payload,'$.expires')<=? ORDER BY p.sequence LIMIT ?''',
            (site_id, high, cutoff, min(limit, self.service.limits.query_events))),
            limit, self.service.limits.query_bytes)

    def digest(self, rows):
        return hashlib.sha256(json.dumps([r['payload'] for r in rows]).encode()).hexdigest()

    def cleanup_preview(self, site_id, limit=100):
        self.service.site(site_id)
        self.limit(limit)
        with self.db.connect() as conn:
            conn.execute('BEGIN')
            high = conn.execute('SELECT COALESCE(MAX(sequence),0) FROM import_positions').fetchone()[0]
            cutoff = time.time()
            rows = self.selection(conn, site_id, cutoff, high, limit)
        state = dict(scope=dict(resource='import-cleanup', site=site_id), high=high, cutoff=cutoff,
                     limit=limit, count=len(rows), digest=self.digest(rows), expires=time.time()+900)
        return self.output(dict(count=len(rows), cutoff=cutoff, cursor=encode(state, self.secret),
                                preserved='committed receipts and unexpired previews'))

    def cleanup_execute(self, site_id, cursor, confirm):
        if confirm != 'delete-expired-import-previews':
            raise ValueError('explicit delete-expired-import-previews confirmation required')
        state = decode(cursor, self.secret, dict(resource='import-cleanup', site=site_id))
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if time.time() < state['cutoff']:
                raise Conflict('clock moved backwards; preview again')
            rows = self.selection(conn, site_id, state['cutoff'], state['high'], state['limit'])
            if len(rows) != state['count'] or self.digest(rows) != state['digest']:
                raise Conflict('cleanup selection changed; preview again')
            result = self.output(dict(deleted=len(rows), cutoff=state['cutoff']))
            for row in rows:
                conn.execute('DELETE FROM sample_imports WHERE site_id=? AND id=?', (site_id, row['id']))
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['list', 'cleanup-preview', 'cleanup-execute'])
    parser.add_argument('--db', required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--cursor')
    parser.add_argument('--confirm')
    args = parser.parse_args(argv)
    try:
        if not Path(args.db).is_file():
            raise ValueError('existing database required')
        history = History(Service(Database.from_env(args.db), Limits.from_env()), os.getenv('GEO_API_TOKEN', ''))
        if args.action == 'list':
            result = history.page(args.site, args.limit, args.cursor)
        elif args.action == 'cleanup-preview':
            result = history.cleanup_preview(args.site, args.limit)
        else:
            result = history.cleanup_execute(args.site, args.cursor, args.confirm)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, sqlite3.Error, ValueError, LookupError, CapacityExceeded):
        print(json.dumps(dict(error='import_history_operation_failed', action=args.action)))
        return 1


if __name__ == '__main__':
    sys.exit(main())
