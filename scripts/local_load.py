"""Repeatable local service-layer workload; ephemeral synthetic data, no network."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geo.db import Database
from geo.service import Service
from geo.paging import review_page


def run(seed, rounds):
    with tempfile.TemporaryDirectory(prefix='xing-local-load-') as directory:
        service = Service(Database(Path(directory) / 'synthetic.sqlite3'))
        sid = service.create_site(dict(name='synthetic', url='https://load.example', brands=['Go']))['id']

        def ingest(ids):
            service.ingest(sid, 'answers', dict(adapter='manual-v1', source='load', records=[
                dict(event_id=i, observed_at='2026-10-29T00:00:00Z', platform='synthetic',
                     prompt='Synthetic local workload', answer='Go local fixture') for i in ids]))

        for offset in range(0, seed, 400):
            ingest([f'seed-{i}' for i in range(offset, min(offset+400, seed))])
        config = service.brands.configuration(sid)
        for i in range(120):
            service.brands.review(sid, dict(config_id=config['id'], brand_id=config['brands'][0]['id'],
                source='load', event_id='seed-0', included=bool(i % 2), reason='Synthetic load review'))

        barrier = threading.Barrier(4)
        def worker(index):
            samples = []
            barrier.wait(timeout=10)
            for iteration in range(rounds):
                start = time.perf_counter()
                if index < 2:
                    ingest([f'writer-{index}-{iteration}-{i}' for i in range(20)])
                    samples.append(('ingest_20', time.perf_counter()-start))
                else:
                    report = service.report(sid)
                    assert seed <= report['answer_samples'] <= seed+rounds*40
                    samples.append(('report', time.perf_counter()-start))
                    start = time.perf_counter()
                    result = review_page(service, sid, 'synthetic-local-cursor-secret', limit=50)
                    assert len(result['items']) == 50
                    samples.append(('review_page_50', time.perf_counter()-start))
            return samples

        tracemalloc.start()
        started = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                samples = [s for group in pool.map(worker, range(4)) for s in group]
            elapsed = time.perf_counter()-started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        final = service.report(sid)['answer_samples']
        assert final == seed + rounds*40
        timings = {}
        for operation in sorted({s[0] for s in samples}):
            values = sorted(seconds*1000 for name, seconds in samples if name == operation)
            timings[operation] = dict(count=len(values), median_ms=round(statistics.median(values), 2),
                p95_ms=round(values[max(0, (len(values)*95+99)//100-1)], 2), max_ms=round(max(values), 2))
        return dict(python=platform.python_version(), os=platform.system(), workers=4, seed_events=seed,
                    rounds_per_worker=rounds, final_events=final, reviews=120, errors=0,
                    elapsed_seconds=round(elapsed, 3), python_traced_peak_bytes=peak, timings=timings,
                    scope='service layer, synthetic temporary SQLite; tracing overhead included; not HTTP or process RSS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=600)
    parser.add_argument('--rounds', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.seed <= 2000 or not 1 <= args.rounds <= 100:
        parser.error('seed must be 1..2000 and rounds 1..100')
    print(json.dumps(run(args.seed, args.rounds), indent=2))
