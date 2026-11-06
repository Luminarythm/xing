"""One-shot Nginx JSONL collector with durable outbox and rename rotation support."""
import argparse
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .adapters import NginxJsonAdapter


@contextmanager
def state_lock(path):
    lock = open(str(path) + '.lock', 'a+b')
    try:
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            if not lock.read(1):
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        lock.close()


def save(path, state):
    temp = Path(str(path) + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(state, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_sender(endpoint, site_id, token):
    p = urlsplit(endpoint)
    if p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
        raise ValueError('endpoint must be an origin without credentials')
    local = False
    try:
        local = ipaddress.ip_address(p.hostname).is_loopback
    except ValueError:
        pass
    if not p.hostname or not (p.scheme == 'https' or (p.scheme == 'http' and local)):
        raise ValueError('collector requires HTTPS, except for loopback HTTP')
    # Never forward a credential to a redirect target.
    opener = build_opener(NoRedirect())

    def send(body):
        request = Request(endpoint.rstrip('/') + '/api/v1/sites/' + site_id + '/logs',
            data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + token,
                                                    'Content-Type': 'application/json'})
        with opener.open(request, timeout=10) as response:
            result = json.load(response)
        if result.get('inserted', 0) + result.get('duplicates', 0) != len(body['records']):
            raise ValueError('server acknowledgement count mismatch')
        return result
    return send


def collect(paths, state_path, binding, send):
    """A state file belongs to exactly one endpoint/site/source; preserve it across runs."""
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_lock(state_path):
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {
            'version': 1, 'binding': binding, 'files': {}, 'pending': None}
        if state.get('version') != 1 or state.get('binding') != binding:
            raise ValueError('checkpoint version or destination mismatch')
        result = dict(acknowledged=0, invalid_lines=0, incomplete_lines=0)

        def flush():
            pending = state['pending']
            if pending is None:
                return
            send(pending['body'])
            state['files'][pending['file']] = pending['next']
            state['pending'] = None
            save(state_path, state)
            result['acknowledged'] += len(pending['body']['records'])

        flush()  # Replay the exact durable batch before reading any new file bytes.
        seen = set()
        for path in sorted(map(Path, paths), key=lambda p: str(p)):
            with path.open('rb') as stream:
                info = os.fstat(stream.fileno())
                identity = f'{info.st_dev}:{info.st_ino}'
                if identity in seen:
                    continue
                seen.add(identity)
                if identity not in state['files']:
                    state['files'][identity] = dict(generation=str(uuid.uuid4()), offset=0, prefix_length=0, prefix='')
                    save(state_path, state)
                position = state['files'][identity]
                prefix = stream.read(position['prefix_length'])
                if info.st_size < position['offset'] or (position['prefix_length'] and hashlib.sha256(prefix).hexdigest() != position['prefix']):
                    raise ValueError('file truncated/replaced in place; restore rotated file or use a new checkpoint explicitly')
                stream.seek(position['offset'])
                batch = []
                end = position['offset']
                while True:
                    offset = stream.tell()
                    line = stream.readline(65537)
                    if not line:
                        break
                    if len(line) > 65536:
                        raise ValueError('log line exceeds 64 KiB; checkpoint unchanged for that line')
                    if not line.endswith(b'\n'):
                        result['incomplete_lines'] += 1
                        break
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError('not an object')
                        row['request_id'] = position['generation'] + ':' + str(offset)
                        normalized = NginxJsonAdapter().normalize(row)
                        batch.append(dict(request_id=normalized['event_id'], time_iso8601=normalized['observed_at'],
                            url=normalized['url'], status=normalized['status'], http_user_agent=normalized['user_agent']))
                        end = stream.tell()
                    except (ValueError, TypeError, KeyError, UnicodeError):
                        result['invalid_lines'] += 1
                        stream.seek(offset)
                        break  # Do not silently skip malformed data.
                    if len(batch) >= 100:
                        break
                if batch:
                    stream.seek(0)
                    prefix = stream.read(min(end, 4096))
                    next_position = dict(position, offset=end, prefix_length=len(prefix), prefix=hashlib.sha256(prefix).hexdigest())
                    state['pending'] = dict(file=identity, next=next_position,
                        body=dict(adapter='nginx-json-v1', source=binding['source'], records=batch))
                    save(state_path, state)
                    flush()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8000')
    parser.add_argument('--site', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('files', nargs='+', help='Current and retained renamed JSONL logs')
    args = parser.parse_args()
    binding = dict(endpoint=args.endpoint.rstrip('/'), site=args.site, source=args.source)
    try:
        result = collect(args.files, args.state, binding,
                         http_sender(args.endpoint, args.site, os.environ['GEO_INGEST_TOKEN']))
        print(json.dumps(result))
        return 2 if result['invalid_lines'] else 0
    except Exception:
        # Fail closed without exposing tokens, raw records or request URLs.
        print(json.dumps({'error': 'collection failed; checkpoint retained; check input, destination and credentials'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
