"""MCP identities come from operator-controlled credentials, never tool arguments."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

import jwt


@dataclass(frozen=True)
class Principal:
    subject: str
    provider: str
    role: str
    project: str
    job_id: str | None = None
    lease_token: str | None = field(default=None, repr=False, compare=False)
    synthetic_probe: bool = False
    diagnose: bool = False


class TokenVault:
    """One trusted service issues per-launch capabilities; restart revokes all."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._tokens = {}
        self._lock = threading.Lock()

    def issue(self, principal: Principal, ttl: int = 600, *, lease_token: str) -> str:
        if principal.diagnose and not principal.synthetic_probe:
            raise ValueError("Diagnostics require a synthetic probe")
        if not principal.job_id or not lease_token or not 1 <= ttl <= 3600:
            raise ValueError("Local access requires a bounded lifetime and a specific leased job")
        if principal.role not in {"management", "audit"} or not principal.subject:
            raise ValueError("Invalid agent identity")
        key = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._tokens = {k: v for k, v in self._tokens.items() if v[0] > now}
            self._tokens[self._hash(key)] = (now + ttl, replace(principal, lease_token=lease_token))
        return key

    @staticmethod
    def _hash(token: str) -> bytes:
        return hashlib.sha256(token.encode()).digest()

    def verify(self, token: str) -> Principal | None:
        with self._lock:
            expiry, principal = self._tokens.get(self._hash(token), (0, None))
            if expiry > self.clock():
                return principal
            self._tokens.pop(self._hash(token), None)
            return None

    def diagnostic_active(self):
        with self._lock:
            now = self.clock()
            return any(expiry > now and p.diagnose for expiry, p in self._tokens.values())

    def revoke(self, token: str):
        with self._lock:
            self._tokens.pop(self._hash(token), None)


class OAuthVerifier:
    """One configured OAuth client and subject bind to one Muse project/role."""

    scope = "sprintrelay:management"

    def __init__(self, issuer: str, jwks_url: str, resource: str, client_id: str,
                 subject: str, project: str, *, key_client=None):
        for url in (issuer, jwks_url, resource):
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                    parsed.password or parsed.query or parsed.fragment):
                raise ValueError("OAuth configuration requires trusted HTTPS URLs")
        if not all((client_id, subject, project)) or urlsplit(resource).path != "/mcp":
            raise ValueError("Configure the OAuth client, subject, project and /mcp audience")
        self.issuer, self.resource, self.client_id = issuer, resource, client_id
        self.subject, self.project = subject, project
        self._keys = key_client or jwt.PyJWKClient(jwks_url, timeout=5, lifespan=300)

    def _verify(self, token: str):
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return None
            key = self._keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                audience=self.resource, options={"require": ["exp", "iss", "aud",
                                                                             "sub", "client_id", "scope"]})
            if claims["client_id"] != self.client_id or claims["sub"] != self.subject:
                return None
            if not isinstance(claims["scope"], str) or self.scope not in claims["scope"].split():
                return None
            return Principal(self.subject, "muse", "management", self.project)
        except (jwt.PyJWTError, OSError, ValueError, TypeError):
            return None

    async def verify(self, token: str):
        return await asyncio.to_thread(self._verify, token)
