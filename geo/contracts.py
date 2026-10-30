"""Validate normalized records at the application boundary."""
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


def text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name}: expected nonempty string <= {limit} characters')
    return value.strip()


def timestamp(value):
    dt = datetime.fromisoformat(text(value, 'timestamp', 50).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    try:
        return dt.astimezone(timezone.utc).isoformat()
    except OverflowError:
        raise ValueError('timestamp outside supported UTC range') from None


def url(value):
    if isinstance(value, str) and any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('URL must not contain whitespace or control characters')
    value = text(value, 'url')
    p = urlsplit(value)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username is not None or p.password is not None:
        raise ValueError('expected absolute http(s) URL without credentials')
    host = p.hostname.encode('idna').decode('ascii').lower()
    if ':' in host:
        host = f'[{host}]'
    port = p.port
    if port == 0:
        raise ValueError('URL port must be between 1 and 65535')
    if port and port != {'https': 443, 'http': 80}[p.scheme]:
        host += f':{port}'
    # Query strings may contain credentials or personal data; never persist them.
    return urlunsplit((p.scheme, host, p.path or '/', '', ''))


def records(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        raise ValueError('records must contain 1..1000 objects')
    if any(not isinstance(x, dict) for x in value):
        raise ValueError('each record must be an object')
    return value
