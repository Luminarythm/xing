"""Export fixed-window reports from one local read snapshot, without raw samples."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import zipfile

from .contracts import timestamp
from .db import SCHEMA_VERSION
from .limits import CapacityExceeded, Limits, json_bytes
from .quality import report as quality_report
from .reporting import FILTERS, normalize_filters
from .service import Service


README = b'''Xing report bundle v1
report.json: basic metrics, brand definitions and page coverage.
quality.json: quality of current retained, deduplicated answer samples.
manifest.json: UTC [start,end), exact filters, selected configuration and inventory,
schema version, limits and SHA-256 digests of the other three files.
All database reads share one SQLite snapshot. Re-exporting later may differ after
imports, reviews, configuration changes or retention. No raw prompts, answers,
event IDs, credentials or database path are included. Site/brand/page metadata
and filter values remain; review them before sharing. Hashes detect accidental
changes, not authenticity. Synthetic labels do not verify real platform exposure.
Missing evidence differs from unknown evidence; see quality metric denominators.
'''


class _SnapshotDatabase:
    """Borrow the export-owned connection; nested readers must not commit it."""
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection


def export_bundle(database, destination, site_id, start, end, *, filters=None,
                  inventory_id=None, brand_config_id=None, limits=None,
                  busy_timeout_ms=2000, query_timeout_ms=5000):
    start, end = timestamp(start), timestamp(end)
    if start >= end:
        raise ValueError('start must precede end')
    filters = normalize_filters(filters)
    if filters.get('kind') == 'logs':
        raise ValueError('bundles require answer quality; kind=logs is unsupported')
    for value in (busy_timeout_ms, query_timeout_ms):
        if type(value) is not int or not 1 <= value <= 60000:
            raise ValueError('database timeouts must be integers from 1 to 60000 ms')
    limits = limits or Limits()
    database, destination = Path(database).resolve(), Path(destination).absolute()
    if not database.is_file():
        raise ValueError('source database does not exist')
    if os.path.lexists(destination):
        raise FileExistsError('destination already exists')
    if not destination.parent.is_dir():
        raise ValueError('destination parent must exist')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True,
                                timeout=busy_timeout_ms / 1000)) as conn:
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + query_timeout_ms / 1000
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.execute('BEGIN')
        # The schema read pins the main database snapshot before any report reads.
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version != SCHEMA_VERSION:
            raise ValueError('export requires current schema; upgrade separately first')
        generated_at = datetime.now(timezone.utc).isoformat()
        snapshot = Service(_SnapshotDatabase(conn), limits)
        basic = snapshot.report(site_id, start, end, inventory_id, filters, brand_config_id)
        quality = quality_report(snapshot, site_id, start, end, filters)
        if basic['answer_samples'] != quality['samples']:
            raise ValueError('report sample counts disagree')
        if time.monotonic() > deadline:
            raise sqlite3.OperationalError('report snapshot deadline exceeded')
        # Closing the connection releases the read transaction and temporary projection.
    files = {'report.json': json_bytes(basic, limits.response_bytes),
             'quality.json': json_bytes(quality, limits.response_bytes), 'README.txt': README}
    manifest = dict(format='xing-report-bundle', version=1, generated_at=generated_at,
        schema_version=version, site_id=basic['site']['id'],
        window=dict(start=start, end=end, end_exclusive=True), filters=filters,
        quality_filters=quality['filters'], quality_dataset=quality['dataset'],
        brand_config_id=basic['brand_config_id'], inventory_id=basic['coverage']['inventory_id'],
        consistency='single_sqlite_read_snapshot', scope='current_retained_data',
        limits=dict(query_events=limits.query_events, query_bytes=limits.query_bytes,
                    uncompressed_bundle_bytes=limits.response_bytes),
        files={name: dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
               for name, data in files.items()})
    files['manifest.json'] = json_bytes(manifest, limits.response_bytes)
    if sum(map(len, files.values())) > limits.response_bytes:
        raise CapacityExceeded('bundle capacity exceeded; narrow the window or filters')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.xing-report-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard link publishes a complete file atomically, refusing replacement.
        # Unsupported filesystems fail closed; never fall back to an overwriting rename.
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return dict(path=str(destination), samples=quality['samples'], files=list(files),
                uncompressed_bytes=sum(map(len, files.values())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('db', 'output', 'site', 'start', 'end'):
        parser.add_argument('--' + name, required=True)
    for name in sorted(FILTERS):
        parser.add_argument('--' + name)
    parser.add_argument('--inventory-id')
    parser.add_argument('--brand-config-id')
    args = parser.parse_args()
    try:
        result = export_bundle(args.db, args.output, args.site, args.start, args.end,
            filters={key: getattr(args, key) for key in FILTERS if getattr(args, key) is not None},
            inventory_id=args.inventory_id, brand_config_id=args.brand_config_id, limits=Limits.from_env(),
            busy_timeout_ms=int(os.getenv('GEO_DB_BUSY_TIMEOUT_MS', '2000')),
            query_timeout_ms=int(os.getenv('GEO_DB_QUERY_TIMEOUT_MS', '5000')))
    except (ValueError, LookupError, OSError, sqlite3.Error, CapacityExceeded) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
