"""Local single-operator API; use a production gateway before external deployment."""
import hmac
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from .service import Conflict
from .security import Security
from .catalog import catalog
from .reporting import FILTERS, events_csv
from .paging import page
from .retention import Retention
from .jobs import Jobs
from .limits import CapacityExceeded, json_bytes
from .status import runtime_status


def server(service, token, host='127.0.0.1', port=8000, security=None):
    if len(token) < 24:
        raise ValueError('GEO_API_TOKEN must contain at least 24 characters')
    security = security or Security(service)
    retention, jobs = Retention(service), Jobs(service)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Avoid writing tokens, prompts or user-supplied URLs to logs.

        def send(self, status, value):
            data = json_bytes(value, service.limits.response_bytes) if self.command == 'GET' and status < 400 else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def send_csv(self, events):
            data = ('\ufeff' + events_csv(events, service.limits.response_bytes)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="xing-events.csv"')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            try:
                p = urlsplit(self.path)
                if self.command == 'GET' and p.path == '/health':
                    return self.send(200, {'status': 'ok'})
                auth = self.headers.get('Authorization', '')
                admin = hmac.compare_digest(auth.encode(), ('Bearer ' + token).encode())
                parts = p.path.strip('/').split('/')
                scoped = (len(parts) == 5 and parts[:3] == ['api', 'v1', 'sites']
                          and parts[4] == 'logs' and self.command == 'POST')
                if not admin:
                    if not (scoped and auth.startswith('Bearer ') and security.permits(auth[7:], parts[3])):
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
                if admin and self.command == 'GET' and p.path == '/api/v1/crawlers':
                    return self.send(200, catalog())
                if admin and self.command == 'GET' and p.path == '/api/v1/status':
                    if p.query:
                        raise ValueError('status does not accept query parameters')
                    try:
                        status = runtime_status(service)
                    except (sqlite3.Error, OSError):
                        return self.send(503, {'error': 'database unavailable'})
                    return self.send(200, status)
                if len(parts) != 5 or parts[:3] != ['api', 'v1', 'sites']:
                    raise LookupError('route not found')
                site_id, action = parts[3:]
                if admin and self.command == 'POST':
                    if action == 'retention-preview':
                        return self.send(200, retention.preview(site_id, body.get('before')))
                    if action == 'retention-execute':
                        return self.send(200, retention.execute(site_id, body.get('plan_id'), body.get('confirm')))
                    if action == 'sample-jobs':
                        return self.send(201, jobs.enqueue(site_id, body))
                    if action == 'brands':
                        return self.send(201, service.brands.configure(site_id, body))
                    if action == 'mention-reviews':
                        return self.send(201, service.brands.review(site_id, body))
                    if action == 'inventory':
                        return self.send(201, service.import_inventory(site_id, body))
                    if action == 'ownership-challenge':
                        return self.send(200, security.challenge(site_id))
                    if action == 'ownership-verify':
                        return self.send(200, security.verify(site_id))
                    if action == 'ingest-keys':
                        return self.send(201, security.issue(site_id))
                    if action == 'revoke-key':
                        return self.send(200, security.revoke(site_id, body.get('key_id')))
                if self.command == 'POST' and action in ('logs', 'answers'):
                    return self.send(200, service.ingest(site_id, action, body))
                query = parse_qs(p.query, keep_blank_values=True)
                allowed = {'report': {'start', 'end', 'inventory_id', 'brand_config_id', *FILTERS},
                           'events': {'start', 'end', 'brand_config_id', 'limit', 'cursor', *FILTERS},
                           'export.csv': {'start', 'end', 'brand_config_id', *FILTERS},
                           'inventory': {'inventory_id'}, 'brands': {'brand_config_id'},
                           'mention-reviews': {'brand_config_id'}}.get(action, set())
                if set(query) - allowed or any(len(v) != 1 or not v[0].strip() for v in query.values()):
                    raise ValueError('unknown, repeated or empty query parameter')
                start, end = query.get('start', [None])[0], query.get('end', [None])[0]
                inventory_id = query.get('inventory_id', [None])[0]
                config_id = query.get('brand_config_id', [None])[0]
                filters = {k: query[k][0] for k in FILTERS if k in query}
                if self.command == 'GET' and action == 'sample-jobs':
                    return self.send(200, jobs.list(site_id))
                if self.command == 'GET' and action == 'brands':
                    return self.send(200, service.brands.configuration(site_id, config_id))
                if self.command == 'GET' and action == 'mention-reviews':
                    config = service.brands.configuration(site_id, config_id)
                    return self.send(200, service.brands.reviews(site_id, config['id']))
                if self.command == 'GET' and action == 'export.csv':
                    return self.send_csv(service.events(site_id, start, end, filters, config_id))
                if self.command == 'GET' and action == 'inventory':
                    return self.send(200, service.inventory(site_id, inventory_id))
                if self.command == 'GET' and action in ('report', 'events'):
                    result = service.report(site_id, start, end, inventory_id, filters, config_id) if action == 'report' else page(
                        service, site_id, token, start, end, filters, config_id,
                        limit=int(query.get('limit', ['100'])[0]), cursor=query.get('cursor', [None])[0])
                    return self.send(200, result)
                raise LookupError('route not found')
            except CapacityExceeded as exc:
                self.send(422, {'error': str(exc), 'code': 'capacity_exceeded'})
            except (Conflict, sqlite3.IntegrityError):
                self.send(409, {'error': 'duplicate site or conflicting event identity'})
            except PermissionError:
                self.send(403, {'error': 'site ownership must be verified'})
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                self.send(400, {'error': str(exc)})
            except LookupError:
                self.send(404, {'error': 'not found'})
            except Exception:
                self.send(500, {'error': 'internal error'})

        do_GET = handle_request
        do_POST = handle_request

    return ThreadingHTTPServer((host, port), Handler)
