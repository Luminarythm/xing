import json
import uuid
import hashlib
from contextlib import nullcontext
from datetime import datetime, timezone
from urllib.parse import urlsplit
from .contracts import text, url, timestamp, records
from .adapters import LOG_ADAPTERS, ANSWER_ADAPTERS, classify
from .catalog import is_ai_event
from .coverage import coverage
from .brands import Brands, classify_mentions, normalize, RULE
from .reporting import normalize_filters, query_predicate
from .limits import Limits, read_rows, json_bytes, CapacityExceeded


class Conflict(ValueError):
    pass


class Service:
    def __init__(self, database, limits=None):
        self.db = database
        self.limits = limits or Limits()
        self.brands = Brands(self)

    def create_site(self, body):
        address = url(body.get('url'))
        p = urlsplit(address)
        brands = body.get('brands')
        if not isinstance(brands, list) or not 1 <= len(brands) <= 50:
            raise ValueError('brands must contain 1..50 names/aliases')
        brands = list(dict.fromkeys(text(b, 'brand', 100) for b in brands))
        if len({normalize(b) for b in brands}) != len(brands):
            raise ValueError('duplicate normalized brand names')
        site = dict(id=str(uuid.uuid4()), name=text(body.get('name'), 'name', 200),
                    origin=f'{p.scheme}://{p.netloc}', brands=brands)
        with self.db.connect() as conn:
            conn.execute('INSERT INTO sites VALUES(?,?,?,?)',
                         (site['id'], site['name'], site['origin'], json.dumps(brands)))
            conn.execute('INSERT INTO site_positions(site_id) VALUES(?)', (site['id'],))
        return site

    def sites(self):
        with self.db.connect() as conn:
            return [dict(r, brands=json.loads(r['brands'])) for r in conn.execute('SELECT * FROM sites')]

    def site(self, site_id):
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM sites WHERE id=?', (site_id,)).fetchone()
        if row is None:
            raise LookupError('site not found')
        return dict(row, brands=json.loads(row['brands']))

    def ingest(self, site_id, kind, body, connection=None):
        if kind not in ('logs', 'answers'):
            raise ValueError('unsupported event kind')
        site = self.site(site_id)
        registry = LOG_ADAPTERS if kind == 'logs' else ANSWER_ADAPTERS
        adapter_name = text(body.get('adapter'), 'adapter', 100)
        if adapter_name not in registry:
            raise ValueError('unsupported adapter')
        source = text(body.get('source'), 'source', 100)
        config = self.brands.configuration(site_id) if kind == 'answers' else None
        normalized = []
        for raw in records(body.get('records')):
            r = registry[adapter_name].normalize(raw)
            r['adapter'] = adapter_name
            if kind == 'logs':
                if urlsplit(r['url']).netloc != urlsplit(site['origin']).netloc:
                    raise ValueError('log URL host must match registered site')
                ua = r.pop('user_agent')
                r['user_agent_hash'] = hashlib.sha256(ua.encode()).hexdigest()
                r.update(classify(ua))
            else:
                r['mentions'] = [d['name'] for d in classify_mentions(r['answer'], config['brands']) if d['included']]
                r['mention_rule'] = RULE
            normalized.append(r)
        inserted = 0
        with (self.db.connect() if connection is None else nullcontext(connection)) as conn:
            if connection is None:
                conn.execute('BEGIN IMMEDIATE')
            watermark = conn.execute('SELECT before FROM retention_watermarks WHERE site_id=?', (site_id,)).fetchone()
            for r in normalized:
                if watermark and r['observed_at'] < watermark['before']:
                    raise ValueError('event predates the retained data window')
                key = (site_id, kind, source, r['event_id'])
                payload = json.dumps(r, ensure_ascii=False, sort_keys=True)
                old = conn.execute('SELECT payload FROM events WHERE site_id=? AND kind=? AND source=? AND event_id=?', key).fetchone()
                if old:
                    previous = json.loads(old['payload'])
                    incoming = dict(r)
                    # Rule changes must not turn a replay into a conflict or rewrite history.
                    for key_name in ('declared_bot', 'verification', 'rule_version', 'mentions', 'mention_rule',
                                     'crawler_category', 'crawler_provider', 'crawler_evidence', 'crawler_source', 'ambiguous_tokens'):
                        previous.pop(key_name, None)
                        incoming.pop(key_name, None)
                    if 'user_agent_hash' not in previous:
                        incoming.pop('user_agent_hash', None)  # v1 did not retain UA evidence.
                    if previous != incoming:
                        raise Conflict('event_id already exists with different content')
                    continue
                conn.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (*key, r['observed_at'], payload))
                conn.execute('INSERT INTO event_positions(site_id,kind,source,event_id) VALUES(?,?,?,?)', key)
                inserted += 1
        return dict(inserted=inserted, duplicates=len(normalized)-inserted)

    def events(self, site_id, start=None, end=None, filters=None, brand_config_id=None):
        self.site(site_id)
        start = timestamp(start) if start else '0001'
        end = timestamp(end) if end else '9999'
        if start >= end:
            raise ValueError('start must precede end')
        clauses, params = query_predicate(site_id, start, end, filters)
        with self.db.connect() as conn:
            rows = read_rows(conn.execute('SELECT e.kind,e.source,e.payload FROM events e WHERE ' +
                ' AND '.join(clauses) + ' ORDER BY e.observed_at,e.kind,e.source,e.event_id', params),
                self.limits.query_events, self.limits.query_bytes)
        events = [dict(kind=r['kind'], source=r['source'], **json.loads(r['payload'])) for r in rows]
        return self.brands.project(site_id, events, self.brands.configuration(site_id, brand_config_id))

    def import_inventory(self, site_id, body):
        site = self.site(site_id)
        incoming = body.get('urls')
        if not isinstance(incoming, list) or len(incoming) > 10000:
            raise ValueError('urls must contain 0..10000 URLs')
        source = text(body.get('source'), 'source', 100)
        urls = sorted(set(url(address) for address in incoming))
        if any(urlsplit(address).netloc != urlsplit(site['origin']).netloc for address in urls):
            raise ValueError('inventory URL host must match registered site')
        item = dict(id=str(uuid.uuid4()), site_id=site_id,
                    created_at=datetime.now(timezone.utc).isoformat(), source=source, urls=urls)
        serialized = json_bytes(urls, self.limits.query_bytes).decode('utf-8')
        # A successful import must have a deliverable response before committing.
        json_bytes(item, self.limits.response_bytes)
        with self.db.connect() as conn:
            conn.execute('INSERT INTO inventories(id,site_id,created_at,source,urls) VALUES(?,?,?,?,?)',
                         (item['id'], site_id, item['created_at'], source, serialized))
        return item

    def inventory(self, site_id, inventory_id=None):
        self.site(site_id)
        with self.db.connect() as conn:
            if not conn.in_transaction:
                conn.execute('BEGIN')
            if inventory_id is None:
                metadata = conn.execute('SELECT id,length(CAST(urls AS BLOB)) AS bytes FROM inventories WHERE site_id=? ORDER BY sequence DESC LIMIT 1', (site_id,)).fetchone()
            else:
                metadata = conn.execute('SELECT id,length(CAST(urls AS BLOB)) AS bytes FROM inventories WHERE site_id=? AND id=?', (site_id, inventory_id)).fetchone()
                if metadata is None:
                    raise LookupError('inventory not found')
            if metadata is None:
                return None
            if metadata['bytes'] > self.limits.query_bytes:
                raise CapacityExceeded('inventory capacity exceeded; use a smaller snapshot or increase the configured budget')
            row = conn.execute('SELECT * FROM inventories WHERE site_id=? AND id=?', (site_id, metadata['id'])).fetchone()
        return dict(row, urls=json.loads(row['urls'])) if row else None

    def report(self, site_id, start=None, end=None, inventory_id=None, filters=None, brand_config_id=None):
        filters = normalize_filters(filters)
        site = self.site(site_id)
        config = self.brands.configuration(site_id, brand_config_id)
        events = self.events(site_id, start, end, filters, config['id'])
        logs = [e for e in events if e['kind'] == 'logs']
        answers = [e for e in events if e['kind'] == 'answers']
        bots = [e for e in logs if is_ai_event(e)]
        inventory = self.inventory(site_id, inventory_id)
        page_coverage = coverage(inventory, bots)
        log_excluded = bool(filters and (filters.get('kind') == 'answers' or any(k in filters for k in ('platform', 'surface', 'model'))))
        if log_excluded:
            for name in ('observed_pages', 'successful_pages', 'unobserved_pages', 'observed_coverage_rate', 'successful_coverage_rate', 'outside_inventory_urls'):
                page_coverage[name] = None
            page_coverage['pages'] = []
        cited = [e for e in answers if any(urlsplit(u).netloc == urlsplit(site['origin']).netloc for u in e['citations'])]
        result = dict(site=site, window=dict(start=start, end=end, end_exclusive=True), filters=filters,
            brand_config_id=config['id'], brand_definitions=config['brands'],
            mention_rule=RULE,
            log_scope='excluded_by_answer_filter' if log_excluded else 'included',
            log_requests=None if log_excluded else len(logs), declared_ai_requests=None if log_excluded else len(bots),
            verified_ai_requests=None if log_excluded else 0,
            failed_ai_requests=None if log_excluded else sum(e['status'] >= 400 for e in bots),
            observed_ai_pages=None if log_excluded else len(set(e['url'] for e in bots)),
            answer_samples=len(answers), mentioned_samples=sum(bool(e['mentions']) for e in answers),
            cited_site_samples=len(cited), citation_rate=len(cited)/len(answers) if answers else None,
            brand_mentions={b['name']: sum(b['name'] in e['mentions'] for e in answers) for b in config['brands']},
            ambiguous_mention_samples=sum(any(d['status'] == 'ambiguous' for d in e['mention_details']) for e in answers),
            evidence_counts_by_surface={s: sum(e['surface'] == s for e in answers) for s in sorted(set(e['surface'] for e in answers))},
            evidence_counts_by_platform={p: sum(e['platform'] == p for e in answers) for p in sorted(set(e['platform'] for e in answers))},
            coverage_rate=page_coverage['observed_coverage_rate'], coverage=page_coverage,
            successful_coverage_rate=page_coverage['successful_coverage_rate'],
            search_engine_requests=None if log_excluded else sum(e.get('crawler_category') == 'search_engine' for e in logs),
            crawler_rule_versions=sorted(set(e['rule_version'] for e in logs)),
            latest_observed_at=max((e['observed_at'] for e in events), default=None),
            limitations=['UA declarations are unverified; crawl != index or citation.',
                        'Coverage uses the selected inventory snapshot and unverified AI declarations; missing/empty inventory yields null.',
                        'Answer ratios describe uploaded samples, not market-wide exposure.',
                        'Literal brand matching may include ambiguous or negative mentions.'])
        answer_excluded = filters.get('kind') == 'logs'
        result['answer_scope'] = 'excluded_by_log_filter' if answer_excluded else 'included'
        if answer_excluded:
            for name in ('answer_samples', 'mentioned_samples', 'cited_site_samples', 'citation_rate',
                         'brand_mentions', 'ambiguous_mention_samples', 'evidence_counts_by_surface', 'evidence_counts_by_platform'):
                result[name] = None
        return result
