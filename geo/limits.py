"""Per-request capacity budgets; exceeding a budget never returns partial results."""
from dataclasses import asdict, dataclass
import json
import os


class CapacityExceeded(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    query_events: int = 10000
    query_bytes: int = 16 * 1024 * 1024
    response_bytes: int = 16 * 1024 * 1024

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < (1 if name == 'query_events' else 1024):
                raise ValueError(name + ' must be a positive integer (bytes >= 1024)')

    @classmethod
    def from_env(cls):
        defaults = cls()
        return cls(**{name: int(os.getenv('GEO_MAX_' + name.upper(), value))
                      for name, value in asdict(defaults).items()})


def read_rows(cursor, max_rows, max_bytes):
    rows, size = [], 0
    for row in cursor:
        size += len(row['payload'].encode('utf-8'))
        if len(rows) >= max_rows or size > max_bytes:
            raise CapacityExceeded('query capacity exceeded; narrow the time window or filters, or use smaller event pages')
        rows.append(row)
    return rows


def json_bytes(value, max_bytes):
    output = bytearray()
    for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(value):
        encoded = chunk.encode('utf-8')
        if len(output) + len(encoded) > max_bytes:
            raise CapacityExceeded('response capacity exceeded; narrow the time window or filters, or use smaller event pages')
        output.extend(encoded)
    return bytes(output)
