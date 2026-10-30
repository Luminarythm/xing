"""Read-only JSONL sample validation. No database, HTTP calls or normalized-content output."""
import argparse
import json
import sys
from .adapters import ManualAnswerAdapter
from .contracts import text
from .quality import is_unknown_model

REQUIRED = {'event_id', 'observed_at', 'platform', 'model', 'surface', 'locale', 'prompt', 'answer', 'citations'}
ALLOWED = REQUIRED | {'evidence_url'}
MAX_BYTES = 8 * 1024 * 1024
MAX_LINE = 256 * 1024
MAX_ROWS = 10000


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('nonfinite JSON')


def validate(stream, max_bytes=MAX_BYTES, max_line=MAX_LINE, max_rows=MAX_ROWS):
    diagnostics, seen = [], set()
    total_bytes = valid = invalid = duplicates = 0
    complete = True
    fatal = None
    while True:
        raw = stream.readline(min(max_line, max_bytes-total_bytes) + 1)
        if not raw:
            break
        total_bytes += len(raw)
        if total_bytes > max_bytes or len(raw) > max_line or len(diagnostics) >= max_rows:
            complete, fatal = False, 'input_limit_exceeded'
            break
        errors, warnings = [], []
        try:
            decoded = raw.decode('utf-8-sig' if not diagnostics else 'utf-8')
            row = json.loads(decoded, object_pairs_hook=unique_object,
                             parse_constant=reject_constant)
        except (UnicodeError, ValueError, RecursionError):
            errors.append('invalid_json_or_utf8')
            row = None
        if not errors:
            if not isinstance(row, dict) or set(row) != {'source', 'record'} or not isinstance(row.get('record'), dict):
                errors.append('invalid_envelope')
            else:
                record = row['record']
                if REQUIRED - record.keys():
                    errors.append('missing_required_fields')
                if record.keys() - ALLOWED:
                    errors.append('unknown_fields')
                if not errors:
                    try:
                        source = text(row['source'], 'source', 100)
                        normalized = ManualAnswerAdapter().normalize(record)
                        json.dumps(normalized, ensure_ascii=False).encode('utf-8')
                        source.encode('utf-8')
                    except (ValueError, TypeError, UnicodeError, OverflowError):
                        errors.append('invalid_record')
                    else:
                        identity = (source, normalized['event_id'])
                        if identity in seen:
                            errors.append('duplicate_identity')
                            duplicates += 1
                        else:
                            seen.add(identity)
                        if not normalized['evidence_url']:
                            warnings.append('missing_evidence_url')
                        if is_unknown_model(normalized['model']):
                            warnings.append('unknown_model')
                        if any('?' in u or '#' in u for u in [*record['citations'], record.get('evidence_url') or '']):
                            warnings.append('url_query_or_fragment_removed_on_ingest')
        invalid += bool(errors)
        valid += not errors
        diagnostics.append(dict(line=len(diagnostics)+1, errors=errors, warnings=warnings))
    if not diagnostics and complete:
        fatal = 'empty_input'
    return dict(format='xing-samples-jsonl-v1', valid=not invalid and complete and fatal is None,
                complete=complete, checked_rows=len(diagnostics), valid_rows=valid, invalid_rows=invalid,
                duplicate_rows=duplicates, fatal=fatal, rows=diagnostics)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', help='UTF-8 JSONL file; one {source,record} object per physical line')
    args = parser.parse_args(argv)
    try:
        with open(args.path, 'rb') as stream:
            result = validate(stream)
    except OSError:
        print(json.dumps(dict(valid=False, complete=False, fatal='input_unreadable')))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    sys.exit(main())
