"""Shared query semantics and spreadsheet-safe CSV serialization."""
import csv
import io
import json
from .contracts import text
from .limits import CapacityExceeded

FILTERS = ('kind', 'platform', 'surface', 'model', 'source')


def normalize_filters(filters):
    filters = dict(filters or {})
    if set(filters) - set(FILTERS):
        raise ValueError('unknown event filter')
    for key, value in filters.items():
        filters[key] = text(value, key, 100)
    if filters.get('kind') not in (None, 'logs', 'answers'):
        raise ValueError('kind must be logs or answers')
    if filters.get('surface') not in (None, 'manual', 'consumer_ui', 'model_api'):
        raise ValueError('invalid surface')
    answer_scope = any(k in filters for k in ('platform', 'surface', 'model'))
    if answer_scope and filters.get('kind') == 'logs':
        raise ValueError('answer filters cannot be combined with kind=logs')
    return filters


def filter_events(events, filters):
    filters = normalize_filters(filters)
    answer_scope = any(k in filters for k in ('platform', 'surface', 'model'))
    return [e for e in events if (not answer_scope or e['kind'] == 'answers') and
            all(e.get(k) == v for k, v in filters.items())]


def query_predicate(site_id, start, end, filters):
    filters = normalize_filters(filters)
    clauses, params = ['e.site_id=?', 'e.observed_at>=?', 'e.observed_at<?'], [site_id, start, end]
    if any(k in filters for k in ('platform', 'surface', 'model')):
        clauses.append("e.kind='answers'")
    for key, value in filters.items():
        column = 'e.' + key if key in ('kind', 'source') else "json_extract(e.payload,'$." + key + "')"
        clauses.append(column + '=?')
        params.append(value)
    return clauses, params


def safe_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    value = '' if value is None else str(value)
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        value = "'" + value
    return value


def events_csv(events, max_bytes=None):
    columns = ['kind', 'source', 'event_id', 'observed_at', 'platform', 'model', 'surface',
               'url', 'status', 'prompt', 'answer', 'citations', 'mentions', 'mention_details', 'brand_config_id']
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(columns)
    # Include the UTF-8 BOM added by the HTTP download in the byte budget.
    size = 3 + len(output.getvalue().encode('utf-8'))
    if max_bytes is not None and size > max_bytes:
        raise CapacityExceeded('CSV capacity exceeded; narrow the time window or filters')
    chunks = [output.getvalue()]
    for event in events:
        output.seek(0)
        output.truncate(0)
        writer.writerow([safe_cell(event.get(name)) for name in columns])
        row = output.getvalue()
        size += len(row.encode('utf-8'))
        if max_bytes is not None and size > max_bytes:
            raise CapacityExceeded('CSV capacity exceeded; narrow the time window or filters')
        chunks.append(row)
    return ''.join(chunks)
