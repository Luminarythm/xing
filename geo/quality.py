"""Current retained answer quality, with explicit evaluated denominators."""
import json
from .contracts import timestamp
from .reporting import normalize_filters, query_predicate
from .limits import read_rows


def is_unknown_model(value):
    return not isinstance(value, str) or not value.strip() or value.strip().casefold() == 'unknown'


def metric(values):
    known = [v for v in values if v is not None]
    count = sum(known)
    return dict(count=count, evaluated_samples=len(known), unknown_samples=len(values)-len(known),
                rate=count/len(known) if known else None)


def report(service, site_id, start=None, end=None, filters=None, dataset='all'):
    service.site(site_id)
    filters = normalize_filters(filters)
    if filters.get('kind') == 'logs':
        raise ValueError('quality reports only support answers')
    filters['kind'] = 'answers'
    if dataset not in ('all', 'synthetic', 'unclassified'):
        raise ValueError('dataset must be all, synthetic or unclassified')
    begin, finish = timestamp(start) if start else '0001', timestamp(end) if end else '9999'
    if begin >= finish:
        raise ValueError('start must precede end')
    clauses, params = query_predicate(site_id, begin, finish, filters)
    synthetic = "COALESCE(lower(trim(json_extract(e.payload,'$.platform')))='synthetic',0)"
    if dataset != 'all':
        clauses.append(synthetic + ('=1' if dataset == 'synthetic' else '=0'))
    with service.db.connect() as conn:
        rows = read_rows(conn.execute('''SELECT json_object(
            'platform',json_extract(e.payload,'$.platform'), 'surface',json_extract(e.payload,'$.surface'),
            'model',json_extract(e.payload,'$.model'), 'observed_at',e.observed_at,
            'citation_count',CASE WHEN json_type(e.payload,'$.citations')='array'
                THEN json_array_length(e.payload,'$.citations') ELSE NULL END,
            'has_evidence',CASE WHEN json_type(e.payload,'$.evidence_url')='null' THEN 0
                WHEN json_type(e.payload,'$.evidence_url')='text' THEN length(trim(json_extract(e.payload,'$.evidence_url')))>0
                ELSE NULL END) AS payload FROM events e WHERE ''' + ' AND '.join(clauses) +
            ' ORDER BY e.observed_at,e.source,e.event_id', params), service.limits.query_events, service.limits.query_bytes)
    answers = [json.loads(r['payload']) for r in rows]
    synthetic_count = sum(isinstance(e['platform'], str) and e['platform'].strip().lower() == 'synthetic' for e in answers)
    surfaces = {s: sum(e['surface'] == s for e in answers) for s in ('manual', 'consumer_ui', 'model_api')}
    surfaces['unknown'] = len(answers)-sum(surfaces.values())
    return dict(site_id=site_id, scope='current_retained_answers', dataset=dataset, filters=filters,
        window=dict(start=begin if start else None, end=finish if end else None, end_exclusive=True),
        samples=len(answers), synthetic_samples=synthetic_count, unclassified_samples=len(answers)-synthetic_count,
        without_citations=metric([None if e['citation_count'] is None else e['citation_count'] == 0 for e in answers]),
        without_evidence_url=metric([None if e['has_evidence'] is None else not e['has_evidence'] for e in answers]),
        unknown_model=metric([is_unknown_model(e['model']) for e in answers]), by_surface=surfaces,
        latest_observed_at=max((e['observed_at'] for e in answers), default=None),
        limitations=['Synthetic means platform is explicitly labelled synthetic; unclassified does not mean verified real.',
                     'URL presence is not proof of authenticity, accessibility or correctness.',
                     'This is a current database query, not an immutable import-file quality snapshot.'])
