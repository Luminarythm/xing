import hashlib
import secrets
import time
import uuid
import http.client
from .ownership import CHALLENGE_PATH, verify_https


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Security:
    def __init__(self, service, verifier=verify_https, clock=time.time):
        self.service, self.db, self.verifier, self.clock = service, service.db, verifier, clock

    def challenge(self, site_id):
        site = self.service.site(site_id)
        token = 'xing-' + secrets.token_urlsafe(32)
        expires = self.clock() + 1800
        with self.db.connect() as conn:
            conn.execute('''INSERT INTO site_security(site_id,challenge,expires_at) VALUES(?,?,?)
                ON CONFLICT(site_id) DO UPDATE SET challenge=excluded.challenge,expires_at=excluded.expires_at''',
                (site_id, token, expires))
        return dict(url=site['origin'] + CHALLENGE_PATH, content=token, expires_at=expires)

    def verify(self, site_id):
        site = self.service.site(site_id)
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM site_security WHERE site_id=?', (site_id,)).fetchone()
        if not row or not row['challenge'] or row['expires_at'] <= self.clock():
            raise ValueError('missing or expired challenge')
        try:
            valid = self.verifier(site['origin'], row['challenge'])
        except (OSError, ValueError, http.client.HTTPException):
            raise ValueError('verification could not retrieve public HTTPS proof') from None
        if not valid:
            raise ValueError('ownership proof did not match')
        until = self.clock() + 30 * 86400
        with self.db.connect() as conn:
            updated = conn.execute('''UPDATE site_security SET verified_until=?,challenge=NULL
                WHERE site_id=? AND challenge=? AND expires_at>?''',
                (until, site_id, row['challenge'], self.clock())).rowcount
            if not updated:
                raise ValueError('challenge changed or expired; retry verification')
        return dict(verified=True, verified_until=until)

    def issue(self, site_id):
        self.service.site(site_id)
        key = 'xing_ingest_' + secrets.token_urlsafe(32)
        key_id = str(uuid.uuid4())
        with self.db.connect() as conn:
            row = conn.execute('SELECT verified_until FROM site_security WHERE site_id=?', (site_id,)).fetchone()
            if not row or row['verified_until'] <= self.clock():
                raise PermissionError('site ownership must be verified')
            conn.execute('INSERT INTO ingest_keys VALUES(?,?,?,?,0)', (key_id, site_id, digest(key), self.clock()))
        return dict(id=key_id, token=key, site_id=site_id)

    def revoke(self, site_id, key_id):
        with self.db.connect() as conn:
            if not conn.execute('UPDATE ingest_keys SET revoked=1 WHERE site_id=? AND id=?', (site_id, key_id)).rowcount:
                raise LookupError('key not found')
        return dict(revoked=True)

    def permits(self, token, site_id):
        with self.db.connect() as conn:
            return conn.execute('''SELECT 1 FROM ingest_keys k JOIN site_security s ON k.site_id=s.site_id
                WHERE k.digest=? AND k.site_id=? AND k.revoked=0 AND s.verified_until>?''',
                (digest(token), site_id, self.clock())).fetchone() is not None
