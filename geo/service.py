import json
import uuid
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlsplit
from .contracts import text, url, timestamp, records
from .adapters import LOG_ADAPTERS, ANSWER_ADAPTERS, classify
from .catalog import is_ai_event
from .coverage import coverage


class Conflict(ValueError):
    pass


class Service:
    def __init__(self, database):
        self.db = database

    def create_site(self, body):
        address = url(body.get('url'))
        p = urlsplit(address)
        brands = body.get('brands')
        if not isinstance(brands, list) or not 1 <= len(brands) <= 50:
            raise ValueError('brands must contain 1..50 names/aliases')
        brands = list(dict.fromkeys(text(b, 'brand', 100) for b in brands))
        site = dict(id=str(uuid.uuid4()), name=text(body.get('name'), 'name', 200),
                    origin=f'{p.scheme}://{p.netloc}', brands=brands)
        with self.db.connect() as conn:
            conn.execute('INSERT INTO sites VALUES(?,?,?,?)',
                         (site['id'], site['name'], site['origin'], json.dumps(brands)))
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

    def ingest(self, site_id, kind, body):
        if kind not in ('logs', 'answers'):
            raise ValueError('unsupported event kind')
        site = self.site(site_id)
        registry = LOG_ADAPTERS if kind == 'logs' else ANSWER_ADAPTERS
        adapter_name = text(body.get('adapter'), 'adapter', 100)
        if adapter_name not in registry:
            raise ValueError('unsupported adapter')
        source = text(body.get('source'), 'source', 100)
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
                r['mentions'] = [b for b in site['brands'] if b.casefold() in r['answer'].casefold()]
                r['mention_rule'] = 'literal-casefold-v1'
            normalized.append(r)
        inserted = 0
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for r in normalized:
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
                inserted += 1
        return dict(inserted=inserted, duplicates=len(normalized)-inserted)

    def events(self, site_id, start=None, end=None):
        self.site(site_id)
        start = timestamp(start) if start else '0001'
        end = timestamp(end) if end else '9999'
        if start >= end:
            raise ValueError('start must precede end')
        with self.db.connect() as conn:
            rows = conn.execute('SELECT kind,source,payload FROM events WHERE site_id=? AND observed_at>=? AND observed_at<? ORDER BY observed_at', (site_id, start, end)).fetchall()
        return [dict(kind=r['kind'], source=r['source'], **json.loads(r['payload'])) for r in rows]

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
        with self.db.connect() as conn:
            conn.execute('INSERT INTO inventories(id,site_id,created_at,source,urls) VALUES(?,?,?,?,?)',
                         (item['id'], site_id, item['created_at'], source, json.dumps(urls)))
        return item

    def inventory(self, site_id, inventory_id=None):
        self.site(site_id)
        with self.db.connect() as conn:
            if inventory_id is None:
                row = conn.execute('SELECT * FROM inventories WHERE site_id=? ORDER BY sequence DESC LIMIT 1', (site_id,)).fetchone()
            else:
                row = conn.execute('SELECT * FROM inventories WHERE site_id=? AND id=?', (site_id, inventory_id)).fetchone()
                if row is None:
                    raise LookupError('inventory not found')
        return dict(row, urls=json.loads(row['urls'])) if row else None

    def report(self, site_id, start=None, end=None, inventory_id=None):
        site = self.site(site_id)
        events = self.events(site_id, start, end)
        logs = [e for e in events if e['kind'] == 'logs']
        answers = [e for e in events if e['kind'] == 'answers']
        bots = [e for e in logs if is_ai_event(e)]
        inventory = self.inventory(site_id, inventory_id)
        page_coverage = coverage(inventory, bots)
        cited = [e for e in answers if any(urlsplit(u).netloc == urlsplit(site['origin']).netloc for u in e['citations'])]
        return dict(site=site, window=dict(start=start, end=end, end_exclusive=True),
            log_requests=len(logs), declared_ai_requests=len(bots), verified_ai_requests=0,
            failed_ai_requests=sum(e['status'] >= 400 for e in bots),
            observed_ai_pages=len(set(e['url'] for e in bots)),
            answer_samples=len(answers), mentioned_samples=sum(bool(e['mentions']) for e in answers),
            cited_site_samples=len(cited), citation_rate=len(cited)/len(answers) if answers else None,
            brand_mentions={b: sum(b in e['mentions'] for e in answers) for b in site['brands']},
            evidence_counts_by_platform={p: sum(e['platform'] == p for e in answers) for p in sorted(set(e['platform'] for e in answers))},
            coverage_rate=page_coverage['observed_coverage_rate'], coverage=page_coverage,
            successful_coverage_rate=page_coverage['successful_coverage_rate'],
            search_engine_requests=sum(e.get('crawler_category') == 'search_engine' for e in logs),
            crawler_rule_versions=sorted(set(e['rule_version'] for e in logs)),
            latest_observed_at=max((e['observed_at'] for e in events), default=None),
            limitations=['UA declarations are unverified; crawl != index or citation.',
                        'Coverage uses the selected inventory snapshot and unverified AI declarations; missing/empty inventory yields null.',
                        'Answer ratios describe uploaded samples, not market-wide exposure.',
                        'Literal brand matching may include ambiguous or negative mentions.'])
