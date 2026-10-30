"""Signed, expiring cursors bound to a caller-defined resource scope."""
import base64
import hashlib
import hmac
import json
import time


def decode(cursor, secret, scope):
    try:
        if not isinstance(cursor, str) or len(cursor) > 8000:
            raise ValueError()
        encoded, signature = cursor.split('.')
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        state = json.loads(base64.urlsafe_b64decode(encoded))
        if state['scope'] != scope or state['expires'] < time.time():
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError):
        raise ValueError('invalid, expired or mismatched cursor') from None


def encode(state, secret):
    encoded = base64.urlsafe_b64encode(json.dumps(state, sort_keys=True).encode()).decode()
    return encoded + '.' + hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
