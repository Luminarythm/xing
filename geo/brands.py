"""Versioned brand definitions and deterministic, reviewable mention matching."""
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from .contracts import text
from .limits import read_rows

RULE = 'alias-boundary-v1'


def normalize(value):
    return unicodedata.normalize('NFKC', value).casefold()


def matches(answer, alias):
    answer, alias = normalize(answer), normalize(alias)
    if alias.isascii():
        return re.search(r'(?<![a-z0-9_])' + re.escape(alias) + r'(?![a-z0-9_])', answer) is not None
    return alias in answer


def classify_mentions(answer, brands):
    owners = {}
    for brand in brands:
        for alias in [brand['name'], *brand['aliases']]:
            owners.setdefault(normalize(alias), set()).add(brand['id'])
    found = {alias: ids for alias, ids in owners.items() if matches(answer, alias)}
    result = []
    for brand in brands:
        unique = [a for a, ids in found.items() if ids == {brand['id']}]
        ambiguous = [a for a, ids in found.items() if brand['id'] in ids and len(ids) > 1]
        result.append(dict(brand_id=brand['id'], name=brand['name'], role=brand['role'],
                           included=bool(unique), status='matched' if unique else 'ambiguous' if ambiguous else 'absent',
                           matched_aliases=unique, ambiguous_aliases=ambiguous))
    return result


class Brands:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def configuration(self, site_id, config_id=None):
        site = self.service.site(site_id)
        with self.db.connect() as conn:
            if config_id in (None, 'legacy'):
                row = None if config_id == 'legacy' else conn.execute(
                    'SELECT * FROM brand_configs WHERE site_id=? ORDER BY sequence DESC LIMIT 1', (site_id,)).fetchone()
            else:
                row = conn.execute('SELECT * FROM brand_configs WHERE site_id=? AND id=?', (site_id, config_id)).fetchone()
                if row is None:
                    raise LookupError('brand configuration not found')
        if row:
            return dict(row, brands=json.loads(row['brands']))
        return dict(id='legacy', site_id=site_id, created_at=None, brands=[
            dict(id=str(uuid.uuid5(uuid.NAMESPACE_URL, site_id + ':' + name)), name=name, aliases=[], role='unspecified')
            for name in site['brands']])

    def configure(self, site_id, body):
        previous = self.configuration(site_id)
        rows = body.get('brands')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
            raise ValueError('brands must contain 1..50 definitions')
        existing = {b['id'] for b in previous['brands']}
        brands, seen_names, seen_ids = [], set(), set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('brand definition must be an object')
            name = text(row.get('name'), 'name', 100)
            brand_id = text(row['id'], 'id', 100) if 'id' in row else str(uuid.uuid4())
            if 'id' in row and brand_id not in existing:
                raise ValueError('id must belong to current site configuration; omit id for a new brand')
            if normalize(name) in seen_names or brand_id in seen_ids:
                raise ValueError('duplicate normalized name or brand id')
            role = row.get('role', 'own')
            if role not in ('own', 'competitor', 'unspecified'):
                raise ValueError('invalid brand role')
            aliases = row.get('aliases', [])
            if not isinstance(aliases, list) or len(aliases) > 50:
                raise ValueError('aliases must contain 0..50 strings')
            aliases = sorted(set(text(a, 'alias', 100) for a in aliases))
            seen_names.add(normalize(name))
            seen_ids.add(brand_id)
            brands.append(dict(id=brand_id, name=name, role=role, aliases=aliases))
        item = dict(id=str(uuid.uuid4()), site_id=site_id, created_at=datetime.now(timezone.utc).isoformat(), brands=brands)
        with self.db.connect() as conn:
            conn.execute('INSERT INTO brand_configs(id,site_id,created_at,brands) VALUES(?,?,?,?)',
                         (item['id'], site_id, item['created_at'], json.dumps(brands, ensure_ascii=False)))
        return item

    def review(self, site_id, body):
        config = self.configuration(site_id, text(body.get('config_id'), 'config_id', 100))
        brand_id = text(body.get('brand_id'), 'brand_id', 100)
        if brand_id not in {b['id'] for b in config['brands']}:
            raise ValueError('brand not in selected configuration')
        included = body.get('included')
        if type(included) is not bool:
            raise ValueError('included must be boolean')
        item = dict(id=str(uuid.uuid4()), site_id=site_id, config_id=config['id'], brand_id=brand_id,
                    source=text(body.get('source'), 'source', 100), event_id=text(body.get('event_id'), 'event_id', 200),
                    included=included, reason=text(body.get('reason'), 'reason', 2000),
                    actor='instance_admin', created_at=datetime.now(timezone.utc).isoformat())
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute("SELECT 1 FROM events WHERE site_id=? AND kind='answers' AND source=? AND event_id=?",
                                (site_id, item['source'], item['event_id'])).fetchone():
                raise LookupError('answer not found')
            conn.execute('INSERT INTO mention_reviews(site_id,config_id,payload) VALUES(?,?,?)',
                         (site_id, config['id'], json.dumps(item, ensure_ascii=False)))
        return item

    def reviews(self, site_id, config_id, max_sequence=9223372036854775807):
        self.configuration(site_id, config_id)
        with self.db.connect() as conn:
            rows = read_rows(conn.execute(
                'SELECT payload FROM mention_reviews WHERE site_id=? AND config_id=? AND sequence<=? ORDER BY sequence',
                (site_id, config_id, max_sequence)), self.service.limits.query_events, self.service.limits.query_bytes)
            return [json.loads(r['payload']) for r in rows]

    def project(self, site_id, events, config, max_review_sequence=9223372036854775807):
        answers = [(e['source'], e['event_id']) for e in events if e['kind'] == 'answers']
        overrides = {}
        if answers:
            with self.db.connect() as conn:
                conn.execute('CREATE TEMP TABLE selected_answers(source TEXT,event_id TEXT,PRIMARY KEY(source,event_id))')
                conn.executemany('INSERT INTO selected_answers VALUES(?,?)', answers)
                # Read only the latest decision per selected answer/brand, not the audit history.
                rows = read_rows(conn.execute('''SELECT json_object(
                    'source',json_extract(r.payload,'$.source'), 'event_id',json_extract(r.payload,'$.event_id'),
                    'brand_id',json_extract(r.payload,'$.brand_id'), 'id',json_extract(r.payload,'$.id'),
                    'included',json_extract(r.payload,'$.included')) AS payload
                    FROM mention_reviews r WHERE r.sequence IN (
                        SELECT MAX(m.sequence) FROM mention_reviews m JOIN selected_answers a
                        ON a.source=json_extract(m.payload,'$.source') AND a.event_id=json_extract(m.payload,'$.event_id')
                        WHERE m.site_id=? AND m.config_id=? AND m.sequence<=?
                        GROUP BY a.source,a.event_id,json_extract(m.payload,'$.brand_id'))''',
                    (site_id, config['id'], max_review_sequence)), len(answers) * len(config['brands']), self.service.limits.query_bytes)
                for row in rows:
                    review = json.loads(row['payload'])
                    review['included'] = bool(review['included'])
                    overrides[(review['source'], review['event_id'], review['brand_id'])] = review
        for event in events:
            if event['kind'] != 'answers':
                continue
            event['original_mentions'] = event.get('mentions', [])
            details = classify_mentions(event['answer'], config['brands'])
            for detail in details:
                review = overrides.get((event['source'], event['event_id'], detail['brand_id']))
                if review:
                    detail.update(included=review['included'], status='reviewed', review_id=review['id'])
            event.update(mentions=[d['name'] for d in details if d['included']], mention_details=details,
                         mention_rule=RULE, brand_config_id=config['id'])
        return events
