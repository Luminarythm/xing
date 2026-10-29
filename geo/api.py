"""Local single-operator API; use a production gateway before external deployment."""
import hmac
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from .service import Conflict


def server(service, token, host='127.0.0.1', port=8000):
    if len(token) < 24:
        raise ValueError('GEO_API_TOKEN must contain at least 24 characters')

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Avoid writing tokens, prompts or user-supplied URLs to logs.

        def send(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            try:
                p = urlsplit(self.path)
                if self.command == 'GET' and p.path == '/health':
                    return self.send(200, {'status': 'ok'})
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                    return self.send(401, {'error': 'unauthorized'})
                body = None
                if self.command == 'POST':
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 2_000_000:
                        return self.send(413, {'error': 'body must be 1..2000000 bytes'})
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError('JSON body must be an object')
                if p.path == '/api/v1/sites':
                    return self.send(201, service.create_site(body)) if body is not None else self.send(200, service.sites())
                parts = p.path.strip('/').split('/')
                if len(parts) != 5 or parts[:3] != ['api', 'v1', 'sites']:
                    raise LookupError('route not found')
                site_id, action = parts[3:]
                if self.command == 'POST' and action in ('logs', 'answers'):
                    return self.send(200, service.ingest(site_id, action, body))
                query = parse_qs(p.query)
                start, end = query.get('start', [None])[0], query.get('end', [None])[0]
                if self.command == 'GET' and action in ('report', 'events'):
                    result = service.report(site_id, start, end) if action == 'report' else service.events(site_id, start, end)
                    return self.send(200, result)
                raise LookupError('route not found')
            except (Conflict, sqlite3.IntegrityError):
                self.send(409, {'error': 'duplicate site or conflicting event identity'})
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                self.send(400, {'error': str(exc)})
            except LookupError:
                self.send(404, {'error': 'not found'})
            except Exception:
                self.send(500, {'error': 'internal error'})

        do_GET = handle_request
        do_POST = handle_request

    return ThreadingHTTPServer((host, port), Handler)
