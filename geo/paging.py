"""Signed keyset cursors, bounded SQL reads and pinned brand/review versions."""
import base64
import hashlib
import hmac
import json
import time
from .contracts import timestamp
from .reporting import normalize_filters, query_predicate
from .limits import read_rows


def page(service, site_id, secret, start=None, end=None, filters=None, brand_config_id=None, limit=100, cursor=None):
    service.site(site_id)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('limit must be 1..500')
    filters = normalize_filters(filters)
    start, end = timestamp(start) if start else '0001', timestamp(end) if end else '9999'
    if start >= end:
        raise ValueError('start must precede end')
    scope = dict(site=site_id, start=start, end=end, filters=filters, config=brand_config_id)
    if cursor:
        try:
            encoded, signature = cursor.split('.')
            if len(cursor) > 8000 or not hmac.compare_digest(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest(), signature):
                raise ValueError()
            state = json.loads(base64.urlsafe_b64decode(encoded))
            if state['scope'] != scope or state['expires'] < time.time():
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ValueError('invalid, expired or mismatched cursor') from None
    else:
        config = service.brands.configuration(site_id, brand_config_id)
        with service.db.connect() as conn:
            high = conn.execute('SELECT COALESCE(MAX(sequence),0) FROM event_positions').fetchone()[0]
            review_high = conn.execute('SELECT COALESCE(MAX(sequence),0) FROM mention_reviews').fetchone()[0]
        state = dict(scope=scope, after=0, high=high, reviews=review_high, config=config['id'], expires=time.time()+3600)
    clauses, params = query_predicate(site_id, start, end, filters)
    clauses.extend(['p.sequence>?', 'p.sequence<=?'])
    params.extend([state['after'], state['high']])
    with service.db.connect() as conn:
        rows = read_rows(conn.execute('''SELECT p.sequence,e.kind,e.source,e.payload FROM event_positions p JOIN events e
            ON (e.site_id,e.kind,e.source,e.event_id)=(p.site_id,p.kind,p.source,p.event_id) WHERE ''' +
            ' AND '.join(clauses) + ' ORDER BY p.sequence LIMIT ?', (*params, limit+1)), limit+1, service.limits.query_bytes)
    more = len(rows) > limit
    rows = rows[:limit]
    events = [dict(kind=r['kind'], source=r['source'], **json.loads(r['payload'])) for r in rows]
    events = service.brands.project(site_id, events, service.brands.configuration(site_id, state['config']), state['reviews'])
    next_cursor = None
    if more:
        state['after'] = rows[-1]['sequence']
        encoded = base64.urlsafe_b64encode(json.dumps(state, sort_keys=True).encode()).decode()
        next_cursor = encoded + '.' + hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return dict(items=events, next_cursor=next_cursor, limit=limit, brand_config_id=state['config'],
                snapshot_high_watermark=state['high'], review_high_watermark=state['reviews'], order='ingestion_sequence')
