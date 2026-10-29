import sqlite3
from pathlib import Path
from contextlib import contextmanager

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sites (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL UNIQUE, brands TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 site_id TEXT NOT NULL REFERENCES sites(id), kind TEXT NOT NULL,
 source TEXT NOT NULL, event_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(site_id, kind, source, event_id));
CREATE INDEX IF NOT EXISTS events_window ON events(site_id, observed_at);
PRAGMA user_version=1;
'''


class Database:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError('unsupported database schema version')
            if version == 0:
                conn.executescript(SCHEMA)
            if version < 2:
                conn.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS site_security (
                  site_id TEXT PRIMARY KEY REFERENCES sites(id), challenge TEXT,
                  expires_at REAL, verified_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS ingest_keys (
                  id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES sites(id),
                  digest TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                PRAGMA user_version=2;
                COMMIT;''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()
