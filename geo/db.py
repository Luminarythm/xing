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
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect(create=True) as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
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
            if version < 3:
                conn.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS inventories (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                  site_id TEXT NOT NULL REFERENCES sites(id), created_at TEXT NOT NULL,
                  source TEXT NOT NULL, urls TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS inventories_site ON inventories(site_id,sequence);
                PRAGMA user_version=3;
                COMMIT;''')
            if version < 4:
                conn.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS brand_configs (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                  site_id TEXT NOT NULL REFERENCES sites(id), created_at TEXT NOT NULL, brands TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS brands_site ON brand_configs(site_id,sequence);
                CREATE TABLE IF NOT EXISTS mention_reviews (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL REFERENCES sites(id),
                  config_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS reviews_scope ON mention_reviews(site_id,config_id,sequence);
                PRAGMA user_version=4;
                COMMIT;''')
            if version < 5:
                conn.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS event_positions (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  site_id TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL,
                  UNIQUE(site_id,kind,source,event_id),
                  FOREIGN KEY(site_id,kind,source,event_id) REFERENCES events(site_id,kind,source,event_id) ON DELETE CASCADE);
                INSERT OR IGNORE INTO event_positions(site_id,kind,source,event_id)
                  SELECT site_id,kind,source,event_id FROM events ORDER BY rowid;
                CREATE TABLE IF NOT EXISTS retention_plans(id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES sites(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS retention_watermarks(site_id TEXT PRIMARY KEY REFERENCES sites(id), before TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sample_jobs (
                  id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES sites(id), request_key TEXT NOT NULL,
                  observed_at TEXT NOT NULL, record TEXT NOT NULL, state TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL, lease TEXT, lease_until REAL,
                  error TEXT, UNIQUE(site_id,request_key));
                PRAGMA user_version=5;
                COMMIT;''')

    def connect(self):
        return self._connect()

    @contextmanager
    def _connect(self, create=False):
        # Only explicit initialization may create a new database.
        uri = Path(self.path).as_uri() + ('?mode=rwc' if create else '?mode=rw')
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()
