"""Shared query semantics and spreadsheet-safe CSV serialization."""
import csv
import io
import json
from .contracts import text

FILTERS = ('kind', 'platform', 'surface', 'model', 'source')


def filter_events(events, filters):
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
    return [e for e in events if (not answer_scope or e['kind'] == 'answers') and
            all(e.get(k) == v for k, v in filters.items())]


def safe_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    value = '' if value is None else str(value)
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        value = "'" + value
    return value


def events_csv(events):
    columns = ['kind', 'source', 'event_id', 'observed_at', 'platform', 'model', 'surface',
               'url', 'status', 'prompt', 'answer', 'citations', 'mentions', 'mention_details', 'brand_config_id']
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(columns)
    for event in events:
        writer.writerow([safe_cell(event.get(name)) for name in columns])
    return output.getvalue()
