"""Ephemeral loopback HTTP workload with concurrent readers/writers; no real sites."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
from pathlib import Path
import platform
import secrets
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geo.api import server
from geo.db import Database
from geo.service import Service


def process_peak_bytes():
    if sys.platform == 'win32':
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        read = ctypes.windll.psapi.GetProcessMemoryInfo
        read.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        read.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not read(current(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return counters.PeakWorkingSetSize
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == 'darwin' else peak * 1024


def run(seed, rounds):
    with tempfile.TemporaryDirectory(prefix='xing-http-load-') as directory:
        service = Service(Database(Path(directory) / 'synthetic.sqlite3'))
        token = secrets.token_urlsafe(32)
        app = server(service, token, port=0)
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        def call(path, body=None):
            req = Request(f'http://127.0.0.1:{app.server_port}/api/v1/'+path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer '+token})
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        def batch(ids):
            return dict(adapter='manual-v1', source='http-load', records=[
                dict(event_id=i, observed_at='2026-10-29T00:00:00Z', platform='synthetic',
                     prompt='Synthetic HTTP workload', answer='Go local fixture') for i in ids])
        try:
            sid = call('sites', dict(name='synthetic', url='https://http-load.example', brands=['Go']))['id']
            prefix = 'sites/'+sid+'/'
            for offset in range(0, seed, 400):
                call(prefix+'answers', batch([f'seed-{i}' for i in range(offset, min(offset+400, seed))]))
            call(prefix+'inventory', dict(source='synthetic', urls=[f'https://http-load.example/{i}' for i in range(50)]))
            barrier = threading.Barrier(4)
            def worker(index):
                samples = []
                barrier.wait(timeout=10)
                for iteration in range(rounds):
                    start = time.perf_counter()
                    if index < 2:
                        result = call(prefix+'answers', batch([f'writer-{index}-{iteration}-{i}' for i in range(20)]))
                        assert result['inserted'] == 20
                        samples.append(('http_ingest_20', time.perf_counter()-start))
                    else:
                        report = call(prefix+'report')
                        assert seed <= report['answer_samples'] <= seed+rounds*40
                        samples.append(('http_report', time.perf_counter()-start))
                        start = time.perf_counter()
                        assert call('sites?limit=50')['items'][0]['id'] == sid
                        samples.append(('http_sites_page', time.perf_counter()-start))
                return samples
            tracemalloc.start()
            started = time.perf_counter()
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    samples = [s for group in pool.map(worker, range(4)) for s in group]
                elapsed = time.perf_counter()-started
                _, peak = tracemalloc.get_traced_memory()
                resident_peak = process_peak_bytes()
            finally:
                tracemalloc.stop()
            final = call(prefix+'report')['answer_samples']
            assert final == seed+rounds*40
            replay = call(prefix+'answers', batch([f'writer-0-{rounds-1}-{i}' for i in range(20)]))
            assert replay == {'inserted': 0, 'duplicates': 20}
            timings = {}
            for operation in sorted({s[0] for s in samples}):
                values = sorted(seconds*1000 for name, seconds in samples if name == operation)
                timings[operation] = dict(count=len(values), median_ms=round(statistics.median(values), 2),
                    p95_ms=round(values[max(0, (len(values)*95+99)//100-1)], 2), max_ms=round(max(values), 2))
            return dict(python=platform.python_version(), os=platform.system(), workers=4, seed_events=seed,
                rounds_per_worker=rounds, final_events=final, errors=0, elapsed_seconds=round(elapsed, 3),
                python_traced_peak_bytes=peak, process_lifetime_resident_peak_bytes=resident_peak,
                timings=timings, scope='loopback HTTP; client and server share one process; fresh connections; tracing overhead included')
        finally:
            app.shutdown()
            app.server_close()
            thread.join(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=600)
    parser.add_argument('--rounds', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.seed <= 2000 or not 1 <= args.rounds <= 100:
        parser.error('seed must be 1..2000 and rounds 1..100')
    print(json.dumps(run(args.seed, args.rounds), indent=2))
