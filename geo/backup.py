"""SQLite online backup and restore-to-new-file only; no remote filesystem API."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3


def copy_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('source database does not exist')
    if source == destination:
        raise ValueError('source and destination must differ')
    # Exclusive reservation: never truncate or replace an existing destination.
    with destination.open('xb'):
        pass
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            if original.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('source integrity check failed')
            version = original.execute('PRAGMA user_version').fetchone()[0]
            if version not in (1, 2, 3, 4, 5, 6):
                raise ValueError('unsupported backup schema')
            tables = {r[0] for r in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'sites', 'events'}
            if version >= 2:
                required |= {'site_security', 'ingest_keys'}
            if version >= 3:
                required.add('inventories')
            if version >= 4:
                required |= {'brand_configs', 'mention_reviews'}
            if version >= 5:
                required |= {'event_positions', 'retention_plans', 'retention_watermarks', 'sample_jobs'}
            if version >= 6:
                required.add('site_positions')
            if not required <= tables:
                raise ValueError('backup is missing required Xing tables')
            with closing(sqlite3.connect(destination)) as target:
                original.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or target.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('backup consistency check failed')
        return dict(path=str(destination), schema_version=version)
    except Exception:
        # Only the file reserved above is eligible for cleanup.
        destination.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    print(json.dumps(copy_database(args.source, args.destination)))
