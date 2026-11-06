"""HTTPS ownership challenge with public-address pinning and no redirects."""
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

CHALLENGE_PATH = '/.well-known/xing-verification.txt'


def public_addresses(host):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    parsed = [ipaddress.ip_address(a) for a in addresses]
    if not parsed or any(not a.is_global or a.is_multicast or a.is_reserved for a in parsed):
        raise ValueError('ownership verification requires public IP addresses')
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, port=443, timeout=5, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def verify_https(origin, expected):
    p = urlsplit(origin)
    if p.scheme != 'https' or p.port not in (None, 443):
        raise ValueError('ownership verification requires HTTPS on port 443')
    addresses = public_addresses(p.hostname)
    conn = PinnedHTTPS(p.hostname, addresses[0])
    try:
        conn.request('GET', CHALLENGE_PATH, headers={'Accept': 'text/plain', 'User-Agent': 'XingOwnership/1'})
        response = conn.getresponse()
        # A redirect is deliberately not followed, including same-host redirects.
        return response.status == 200 and response.read(513).strip() == expected.encode('ascii')
    finally:
        conn.close()
