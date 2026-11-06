"""Fixed 1000-sample synthetic export/verification baseline; no network or retained database."""
import json
import argparse
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geo.bundle import export_bundle
from geo.bundle_check import verify
from geo.db import Database
from geo.service import Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expanded', action='store_true', help='Include 5000 pages and 1000 review records')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        service = Service(Database(root / 'data.sqlite3'))
        site = service.create_site(dict(name='Synthetic baseline', url='https://demo.example', brands=['Go']))
        for batch in range(2):
            service.ingest(site['id'], 'answers', dict(adapter='manual-v1', source='synthetic-baseline', records=[
                dict(event_id=str(batch * 500 + i), observed_at='2026-10-30T12:00:00Z',
                     platform='synthetic', surface='manual', model='unknown', prompt='synthetic prompt',
                     answer='Go synthetic answer ' * 50, citations=[]) for i in range(500)]))
        if args.expanded:
            service.import_inventory(site['id'], dict(source='synthetic-baseline',
                urls=['https://demo.example/page/' + str(i) for i in range(5000)]))
            brand = service.brands.configuration(site['id'])['brands'][0]
            for i in range(1000):
                service.brands.review(site['id'], dict(config_id='legacy', brand_id=brand['id'],
                    source='synthetic-baseline', event_id='0', included=bool(i % 2), reason='synthetic baseline'))
        tracemalloc.start()
        started = time.perf_counter()
        result = export_bundle(service.db.path, root / 'report.zip', site['id'],
                               '2026-10-30T00:00:00Z', '2026-10-31T00:00:00Z')
        exported = time.perf_counter()
        checked = verify(root / 'report.zip')
        finished = time.perf_counter()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if checked['quality']['samples'] != 1000:
            raise ValueError('baseline count mismatch')
        print(json.dumps(dict(samples=1000, pages=5000 if args.expanded else 0,
            reviews=1000 if args.expanded else 0, export_seconds=round(exported-started, 4),
            verify_seconds=round(finished-exported, 4), compressed_bytes=(root / 'report.zip').stat().st_size,
            uncompressed_bytes=result['uncompressed_bytes'], python_traced_peak_bytes=peak,
            scope='single process synthetic baseline; traced memory excludes SQLite/native allocations')))


if __name__ == '__main__':
    main()
