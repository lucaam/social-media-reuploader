"""Authentication and authorization for the admin GUI.

There is exactly one authorization function, :func:`resolve_principal`, used
by every HTTP route, by the WebSocket handshake and by ``/api/me``:

* ``Authorization: Bearer <ADMIN_TOKEN>`` (API clients). Compared in constant
  time; a wrong token is an error (401), never a fallback to the cookie.
  Tokens in query strings are never accepted.
* A signed session cookie created by ``POST /login`` (admin token) or by the
  OAuth/OIDC callback. The cookie only carries *identity*; admin rights are
  re-derived on every request:

  - token sessions store an HMAC fingerprint of the token they were created
    with, so rotating ``ADMIN_TOKEN`` invalidates them;
  - OAuth sessions are admin when one of their (stored) groups is in
    ``OAUTH_ADMIN_GROUPS`` or when their verified e-mail maps to a DB user
    with role ``admin`` (looked up on every request, so demotion works).
    The groups are the ones the provider sent at login: the IdP is not asked
    again, so a group change applies from the next login.
    Verified means ``email_verified`` (or Discord's ``verified``) is true;
    an e-mail without any such claim counts only with
    ``OAUTH_ASSUME_EMAIL_VERIFIED=true`` (providers like GitHub that only
    return verified addresses), one marked unverified never does.

Settings are read from the environment by :meth:`AuthSettings.from_env`
(``SECRET_KEY``, ``ADMIN_TOKEN``, ``SESSION_COOKIE_SECURE``,
``SESSION_MAX_AGE_SECONDS``, ``GUI_TRUSTED_ORIGINS`` and the ``OAUTH_*``
variables). Without a usable ``SECRET_KEY`` a random per-process key is used:
cookies are never signed with a public default.

Everything here is synchronous: :func:`resolve_principal` may query the DB,
so async callers run it in a worker thread.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import db

logger = logging.getLogger(__name__)

# The public fallback key of the old GUI: treated exactly like "unset".
LEGACY_DEFAULT_SECRET = "dev-secret-change-me"
MIN_SECRET_KEY_LENGTH = 32
MIN_ADMIN_TOKEN_LENGTH = 16
SESSION_COOKIE_NAME = "smr_session"
SESSION_VERSION = 1
DEFAULT_SESSION_MAX_AGE = 12 * 3600
MIN_SESSION_MAX_AGE = 300
MAX_SESSION_MAX_AGE = 7 * 86400
DEFAULT_OAUTH_SCOPE = "openid profile email"
DEFAULT_OAUTH_TIMEOUT = 10.0
OAUTH_CLIENT_NAME = "provider"
# Failed admin-token attempts allowed per client address and window.
LOGIN_MAX_FAILURES = 10
LOGIN_FAILURE_WINDOW_SECONDS = 300.0
# ... and from all addresses together: behind a proxy that appends to
# X-Forwarded-For, the client chooses the address the per-address limit sees.
LOGIN_MAX_TOTAL_FAILURES = 100
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_GROUP_CLAIMS = ("groups", "memberOf", "member_of")
_MAX_CLAIM_LENGTH = 256
_MAX_GROUPS = 200
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-\[\]:]{1,255}$")
_PREFIX_RE = re.compile(r"^(/[A-Za-z0-9._~\-]+)+$")
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


class AuthMethod(StrEnum):
    BEARER = "bearer"  # Authorization header with ADMIN_TOKEN
    TOKEN = "token"  # session created by POST /login
    OAUTH = "oauth"  # session created by the OAuth/OIDC callback


class AuthError(Exception):
    """Invalid explicit credentials (HTTP 401) or too many attempts (429)."""

    def __init__(
        self, status_code: int, detail: str, *, retry_after: float | None = None
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after

    @property
    def headers(self) -> dict[str, str]:
        if self.retry_after is not None:
            return {"Retry-After": str(max(1, int(self.retry_after + 0.999)))}
        return {"WWW-Authenticate": "Bearer"}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OAuthSettings:
    client_id: str
    client_secret: str
    authorize_url: str | None = None
    token_url: str | None = None
    userinfo_url: str | None = None
    server_metadata_url: str | None = None
    jwks_uri: str | None = None
    issuer: str | None = None
    scope: str = DEFAULT_OAUTH_SCOPE
    redirect_uri: str | None = None
    timeout: float = DEFAULT_OAUTH_TIMEOUT

    def register_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``authlib`` ``OAuth.register``."""
        kwargs: dict[str, Any] = {
            "name": OAUTH_CLIENT_NAME,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "client_kwargs": {"scope": self.scope, "timeout": self.timeout},
        }
        if self.server_metadata_url:
            kwargs["server_metadata_url"] = self.server_metadata_url
        if self.authorize_url:
            kwargs["authorize_url"] = self.authorize_url
        if self.token_url:
            kwargs["access_token_url"] = self.token_url
        # Extra keywords become (initial) server metadata in authlib: they let
        # ID tokens be verified and userinfo be fetched without discovery.
        if self.jwks_uri:
            kwargs["jwks_uri"] = self.jwks_uri
        if self.userinfo_url:
            kwargs["userinfo_endpoint"] = self.userinfo_url
        if self.issuer:
            kwargs["issuer"] = self.issuer
        return kwargs


@dataclass(frozen=True)
class AuthSettings:
    secret_key: str
    secret_key_ephemeral: bool = False
    admin_token: str | None = None
    admin_groups: frozenset[str] = frozenset()  # casefolded
    oauth: OAuthSettings | None = None
    cookie_secure: bool = False
    session_max_age: int = DEFAULT_SESSION_MAX_AGE
    trusted_origins: frozenset[str] = frozenset()  # normalized origins
    # Use OAuth e-mails that come without any verification claim for the DB
    # role (GitHub's /user only returns verified addresses but says nothing).
    assume_email_verified: bool = False
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AuthSettings:
        env = os.environ if environ is None else environ
        warnings: list[str] = []
        secret, ephemeral = _secret_key(env, warnings)
        admin_token = _env(env, "ADMIN_TOKEN")
        if admin_token and len(admin_token) < MIN_ADMIN_TOKEN_LENGTH:
            warnings.append(
                f"ADMIN_TOKEN is shorter than {MIN_ADMIN_TOKEN_LENGTH} characters;"
                " use a long random value"
            )
        oauth = _oauth_settings(env, warnings)
        max_age = _env_int(
            env,
            "SESSION_MAX_AGE_SECONDS",
            DEFAULT_SESSION_MAX_AGE,
            MIN_SESSION_MAX_AGE,
            MAX_SESSION_MAX_AGE,
            warnings,
        )
        trusted = set(parse_origins(_env(env, "GUI_TRUSTED_ORIGINS"), warnings))
        if oauth and oauth.redirect_uri:
            redirect_origin = origin_of(oauth.redirect_uri)
            if redirect_origin:
                trusted.add(redirect_origin)
        groups = (_env(env, "OAUTH_ADMIN_GROUPS") or "").split(",")
        return cls(
            secret_key=secret,
            secret_key_ephemeral=ephemeral,
            admin_token=admin_token,
            admin_groups=frozenset(g.strip().casefold() for g in groups if g.strip()),
            oauth=oauth,
            cookie_secure=_env_bool(env, "SESSION_COOKIE_SECURE", False, warnings),
            session_max_age=max_age,
            trusted_origins=frozenset(trusted),
            assume_email_verified=_env_bool(
                env, "OAUTH_ASSUME_EMAIL_VERIFIED", False, warnings
            ),
            warnings=tuple(warnings),
        )

    @property
    def token_enabled(self) -> bool:
        return bool(self.admin_token)

    def email_trusted(self, verified: bool | None) -> bool:
        """May an OAuth e-mail with this verification flag get its DB role?"""
        return verified is True or (verified is None and self.assume_email_verified)


def _env(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(
    env: Mapping[str, str], name: str, default: bool, warnings: list[str]
) -> bool:
    value = _env(env, name)
    if value is None:
        return default
    low = value.lower()
    if low in _TRUE_WORDS:
        return True
    if low in _FALSE_WORDS:
        return False
    warnings.append(f"{name}={value!r} is not a boolean; using {default}")
    return default


def _env_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    warnings: list[str],
) -> int:
    value = _env(env, name)
    if value is None:
        return default
    try:
        parsed = int(float(value))
    except ValueError:
        warnings.append(f"{name}={value!r} is not a number; using {default}")
        return default
    clamped = min(max(parsed, minimum), maximum)
    if clamped != parsed:
        warnings.append(
            f"{name}={parsed} is outside {minimum}..{maximum}; using {clamped}"
        )
    return clamped


def _secret_key(env: Mapping[str, str], warnings: list[str]) -> tuple[str, bool]:
    secret = _env(env, "SECRET_KEY")
    if secret is None or secret == LEGACY_DEFAULT_SECRET:
        warnings.append(
            "SECRET_KEY is not set (or is the old public default): using a random"
            " per-process key. Sessions do not survive restarts and are not shared"
            " between GUI replicas; set SECRET_KEY to a long random value"
        )
        return secrets.token_urlsafe(48), True
    if len(secret) < MIN_SECRET_KEY_LENGTH:
        warnings.append(
            f"SECRET_KEY is shorter than {MIN_SECRET_KEY_LENGTH} characters;"
            " use a long random value (e.g. `openssl rand -hex 32`)"
        )
    return secret, False


def _oauth_settings(
    env: Mapping[str, str], warnings: list[str]
) -> OAuthSettings | None:
    client_id = _env(env, "OAUTH_CLIENT_ID")
    client_secret = _env(env, "OAUTH_CLIENT_SECRET")
    authorize_url = _env(env, "OAUTH_AUTHORIZE_URL")
    token_url = _env(env, "OAUTH_TOKEN_URL")
    metadata_url = _env(env, "OAUTH_SERVER_METADATA_URL")
    if not any((client_id, client_secret, authorize_url, token_url, metadata_url)):
        return None
    problems = []
    if not (client_id and client_secret):
        problems.append("OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET are both required")
    if not metadata_url and not (authorize_url and token_url):
        problems.append(
            "set OAUTH_SERVER_METADATA_URL, or both OAUTH_AUTHORIZE_URL and"
            " OAUTH_TOKEN_URL"
        )
    if problems or client_id is None or client_secret is None:
        warnings.append("OAuth is disabled: " + "; ".join(problems))
        return None
    redirect_uri = _env(env, "OAUTH_REDIRECT_URI")
    if redirect_uri and origin_of(redirect_uri) is None:
        warnings.append(
            f"OAUTH_REDIRECT_URI={redirect_uri!r} is not an absolute http(s) URL;"
            " ignoring it"
        )
        redirect_uri = None
    scope = _env(env, "OAUTH_SCOPE") or DEFAULT_OAUTH_SCOPE
    jwks_uri = _env(env, "OAUTH_JWKS_URI")
    if "openid" in scope.split() and not metadata_url and not jwks_uri:
        warnings.append(
            "OAUTH_SCOPE contains 'openid' but neither OAUTH_SERVER_METADATA_URL nor"
            " OAUTH_JWKS_URI is set: ID tokens returned by the provider cannot be"
            " verified and such logins will fail"
        )
    timeout_value = _env(env, "OAUTH_TIMEOUT_SECONDS")
    timeout = DEFAULT_OAUTH_TIMEOUT
    if timeout_value is not None:
        try:
            timeout = min(max(float(timeout_value), 1.0), 60.0)
        except ValueError:
            warnings.append(
                f"OAUTH_TIMEOUT_SECONDS={timeout_value!r} is not a number;"
                f" using {DEFAULT_OAUTH_TIMEOUT}"
            )
    return OAuthSettings(
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=authorize_url,
        token_url=token_url,
        userinfo_url=_env(env, "OAUTH_USERINFO_URL"),
        server_metadata_url=metadata_url,
        jwks_uri=jwks_uri,
        issuer=_env(env, "OAUTH_ISSUER"),
        scope=scope,
        redirect_uri=redirect_uri,
        timeout=timeout,
    )


def build_oauth_registry(settings: AuthSettings) -> Any:
    """An authlib ``OAuth`` registry with the provider registered (if any)."""
    from authlib.integrations.starlette_client import OAuth

    registry = OAuth()
    if settings.oauth is not None:
        registry.register(**settings.oauth.register_kwargs())
    return registry


def get_oauth_client(registry: Any) -> Any | None:
    """The registered (or test-injected) OAuth client, or None."""
    client = getattr(registry, OAUTH_CLIENT_NAME, None)
    return client or None


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    """Who is calling and whether they are an administrator."""

    method: AuthMethod | None = None
    is_admin: bool = False
    subject: str | None = None
    email: str | None = None
    name: str | None = None
    username: str | None = None
    groups: tuple[str, ...] = ()
    role: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.method is not None

    @property
    def actor(self) -> str:
        """Short audit label stored with admin actions."""
        if self.method is None:
            return "gui:anonymous"
        if self.method is not AuthMethod.OAUTH:
            return f"gui:{self.method.value}"
        who = self.email or self.username or self.subject or "?"
        return f"gui:oauth:{who}"[:120]

    def public(self) -> dict[str, Any]:
        """JSON for ``/api/me``."""
        user: dict[str, Any] | None = None
        if self.method is AuthMethod.OAUTH:
            user = {
                "sub": self.subject,
                "email": self.email,
                "name": self.name,
                "username": self.username,
                "groups": list(self.groups),
            }
        elif self.method is not None:
            user = {"name": "Amministratore (token)"}
        return {
            "authenticated": self.authenticated,
            "is_admin": self.is_admin,
            "auth": self.method.value if self.method else None,
            "role": self.role,
            "user": user,
        }


ANONYMOUS = Principal()
UserLookup = Callable[[str], Mapping[str, Any] | None]


def bearer_token(headers: Headers) -> str | None:
    """The token of an ``Authorization: Bearer`` header ("" if empty), else None.

    Other schemes (e.g. Basic added by a proxy) are ignored.
    """
    value = headers.get("authorization")
    if not value:
        return None
    scheme, _, credentials = value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip()


def uses_bearer(headers: Headers, settings: AuthSettings) -> bool:
    """True when the request authenticates with the admin token header."""
    return settings.token_enabled and bearer_token(headers) is not None


def tokens_match(candidate: str, expected: str) -> bool:
    """Constant-time comparison (any str, even with lone surrogates from JSON)."""
    return hmac.compare_digest(
        candidate.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def token_fingerprint(settings: AuthSettings) -> str | None:
    """HMAC of the current admin token, stored in token sessions."""
    if not settings.admin_token:
        return None
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        b"smr-admin-token\x00" + settings.admin_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def client_key(conn: HTTPConnection) -> str:
    return conn.client.host if conn.client else "unknown"


def resolve_principal(
    conn: HTTPConnection,
    settings: AuthSettings,
    *,
    oauth_enabled: bool,
    limiter: FailureLimiter | None = None,
    lookup_user: UserLookup | None = None,
) -> Principal:
    """The one admin check (HTTP routes, WebSocket and ``/api/me``).

    Raises :class:`AuthError` for a wrong/throttled bearer token and lets DB
    errors propagate (they are not "not admin").
    """
    if settings.token_enabled:
        token = bearer_token(conn.headers)
        if token is not None:
            return _bearer_principal(token, settings, client_key(conn), limiter)
    session = conn.scope.get("session")
    if not isinstance(session, Mapping):
        return ANONYMOUS
    return session_principal(
        session, settings, oauth_enabled=oauth_enabled, lookup_user=lookup_user
    )


def _bearer_principal(
    token: str, settings: AuthSettings, key: str, limiter: FailureLimiter | None
) -> Principal:
    if limiter is not None:
        retry = limiter.retry_after(key)
        if retry is not None:
            raise AuthError(
                429, "Troppi tentativi falliti: riprova più tardi", retry_after=retry
            )
    expected = settings.admin_token or ""
    if not token or not tokens_match(token, expected):
        if limiter is not None:
            limiter.record_failure(key)
        logger.warning("Rejected an invalid admin bearer token from %s", key)
        raise AuthError(401, "Token non valido")
    return Principal(method=AuthMethod.BEARER, is_admin=True, subject="admin-token")


def session_principal(
    session: Mapping[str, Any],
    settings: AuthSettings,
    *,
    oauth_enabled: bool,
    lookup_user: UserLookup | None = None,
) -> Principal:
    """Principal of a session cookie (anonymous when invalid or expired)."""
    if session.get("v") != SESSION_VERSION or not _session_fresh(session, settings):
        return ANONYMOUS
    kind = session.get("auth")
    if kind == AuthMethod.TOKEN.value:
        expected = token_fingerprint(settings)
        stored = session.get("fp")
        if expected and isinstance(stored, str) and tokens_match(stored, expected):
            return Principal(
                method=AuthMethod.TOKEN, is_admin=True, subject="admin-token"
            )
        return ANONYMOUS
    if kind == AuthMethod.OAUTH.value and oauth_enabled:
        return _oauth_principal(session, settings, lookup_user or db.get_user_by_email)
    return ANONYMOUS


def _session_fresh(session: Mapping[str, Any], settings: AuthSettings) -> bool:
    issued = session.get("iat")
    if not isinstance(issued, int | float):
        return False
    age = time.time() - float(issued)
    return -300 <= age <= settings.session_max_age


def _oauth_principal(
    session: Mapping[str, Any], settings: AuthSettings, lookup_user: UserLookup
) -> Principal:
    groups = tuple(
        group for group in session.get("groups") or () if isinstance(group, str)
    )
    email = _text(session.get("email"))
    by_group = any(group.casefold() in settings.admin_groups for group in groups)
    role: str | None = None
    verified = session.get("ev")
    if not by_group and email and settings.email_trusted(verified):
        user = lookup_user(email)
        role = str(user["role"]) if user and user.get("role") else None
    return Principal(
        method=AuthMethod.OAUTH,
        is_admin=by_group or role == "admin",
        subject=_text(session.get("sub")),
        email=email,
        name=_text(session.get("name")),
        username=_text(session.get("user")),
        groups=groups,
        role=role,
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def start_token_session(
    session: MutableMapping[str, Any], settings: AuthSettings
) -> None:
    """Replace the session with an admin-token session."""
    session.clear()
    session.update(
        {
            "v": SESSION_VERSION,
            "auth": AuthMethod.TOKEN.value,
            "fp": token_fingerprint(settings),
            "iat": int(time.time()),
        }
    )


@dataclass(frozen=True)
class OAuthIdentity:
    subject: str | None
    issuer: str | None
    email: str | None
    email_verified: bool | None
    name: str | None
    username: str | None
    groups: tuple[str, ...]

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> OAuthIdentity | None:
        """Identity from ID-token/userinfo claims; None without any identifier.

        ``sub`` falls back to ``id`` (GitHub). ``email_verified`` falls back to
        ``verified`` (Discord's userinfo); see
        :meth:`AuthSettings.email_trusted` for when the e-mail gives a DB role.
        """
        subject = _text(claims.get("sub")) or _text(claims.get("id"))
        email = _text(claims.get("email"))
        username = (
            _text(claims.get("preferred_username"))
            or _text(claims.get("login"))
            or _text(claims.get("nickname"))
        )
        if not (subject or email or username):
            return None
        verified = claims.get("email_verified")
        if verified is None:
            verified = claims.get("verified")
        return cls(
            subject=subject,
            issuer=_text(claims.get("iss")),
            email=email,
            email_verified=parse_verified(verified),
            name=_text(claims.get("name")),
            username=username,
            groups=tuple(claim_groups(claims)),
        )

    def session_data(self, settings: AuthSettings) -> dict[str, Any]:
        # Only the groups that grant admin are kept: the cookie stays small.
        matched = [g for g in self.groups if g.casefold() in settings.admin_groups]
        return {
            "v": SESSION_VERSION,
            "auth": AuthMethod.OAUTH.value,
            "sub": self.subject,
            "iss": self.issuer,
            "email": self.email,
            "ev": self.email_verified,
            "name": self.name,
            "user": self.username,
            "groups": matched[:20],
            "iat": int(time.time()),
        }


def start_oauth_session(
    session: MutableMapping[str, Any], identity: OAuthIdentity, settings: AuthSettings
) -> None:
    """Replace the session (incl. any OAuth state) with the new identity."""
    session.clear()
    session.update(identity.session_data(settings))


def parse_verified(value: Any) -> bool | None:
    """``email_verified`` claim -> True / False / None (absent)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_WORDS
    return False


def claim_groups(claims: Mapping[str, Any]) -> list[str]:
    raw: Any = None
    for name in _GROUP_CLAIMS:
        raw = claims.get(name)
        if raw:
            break
    if isinstance(raw, str):
        items: Iterable[Any] = raw.split(",")
    elif isinstance(raw, list | tuple):
        items = raw
    else:
        return []
    groups = [str(item).strip() for item in items if isinstance(item, str | int)]
    return [group[:_MAX_CLAIM_LENGTH] for group in groups if group][:_MAX_GROUPS]


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:_MAX_CLAIM_LENGTH] or None


# ---------------------------------------------------------------------------
# Failed-attempt throttling
# ---------------------------------------------------------------------------
class FailureLimiter:
    """Sliding-window counter of failed token attempts per client address,
    plus a budget shared by all addresses (``max_total_failures``).

    The client address can be chosen by the client (proxy headers), so the
    shared budget is what bounds a guessing attack from rotating addresses.
    When it is spent, token logins pause for everyone until the window moves.
    """

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
        max_keys: int = 4096,
        max_total_failures: int = LOGIN_MAX_TOTAL_FAILURES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self.max_total_failures = max_total_failures
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._all_failures: deque[float] = deque()

    def retry_after(self, key: str) -> float | None:
        """Seconds until ``key`` may try again, or None when allowed now."""
        now = self._clock()
        with self._lock:
            waits: list[float] = []
            attempts = self._prune(key, now)
            if attempts is not None and len(attempts) >= self.max_failures:
                waits.append(attempts[0] + self.window_seconds - now)
            spent = self._prune_all(now)
            if spent >= self.max_total_failures:
                oldest_to_expire = self._all_failures[spent - self.max_total_failures]
                waits.append(oldest_to_expire + self.window_seconds - now)
            return max(0.0, *waits) if waits else None

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            attempts = self._prune(key, now)
            if attempts is None:
                attempts = self._failures[key] = deque()
            attempts.append(now)
            self._failures.move_to_end(key)
            while len(self._failures) > self.max_keys:
                self._failures.popitem(last=False)
            self._prune_all(now)
            self._all_failures.append(now)
            if len(self._all_failures) == self.max_total_failures:
                logger.warning(
                    "%d failed admin-token attempts in %.0f s from all client"
                    " addresses: token logins are paused for everyone (spoofed"
                    " X-Forwarded-For addresses?)",
                    self.max_total_failures,
                    self.window_seconds,
                )

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _prune(self, key: str, now: float) -> deque[float] | None:
        attempts = self._failures.get(key)
        if attempts is None:
            return None
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if not attempts:
            del self._failures[key]
            return None
        return attempts

    def _prune_all(self, now: float) -> int:
        """Drop expired entries of the shared budget; how many remain."""
        while self._all_failures and self._all_failures[0] <= now - self.window_seconds:
            self._all_failures.popleft()
        return len(self._all_failures)


# ---------------------------------------------------------------------------
# Origins, CSRF and external URLs
# ---------------------------------------------------------------------------
def origin_of(url: str | None) -> str | None:
    """Normalized ``scheme://host[:port]`` of an absolute http(s) URL."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def parse_origins(raw: str | None, warnings: list[str]) -> list[str]:
    origins: list[str] = []
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        origin = origin_of(item)
        if origin is None:
            warnings.append(
                f"GUI_TRUSTED_ORIGINS entry {item.strip()!r} is not an origin"
            )
        else:
            origins.append(origin)
    return origins


def _host_port(origin: str) -> str:
    return origin.split("://", 1)[1]


def _strip_default_port(netloc: str, scheme: str) -> str:
    netloc = netloc.strip().lower()
    default = _DEFAULT_PORTS.get(scheme)
    suffix = f":{default}"
    if default is not None and netloc.endswith(suffix):
        return netloc[: -len(suffix)]
    return netloc


def _first(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def request_hosts(headers: Headers) -> list[str]:
    """Host names the client may legitimately use for this server."""
    hosts = []
    for name in ("host", "x-forwarded-host"):
        value = headers.get(name)
        if value:
            hosts.extend(
                item.strip().lower() for item in value.split(",") if item.strip()
            )
    return hosts


def origin_allowed(headers: Headers, settings: AuthSettings) -> bool:
    """True when a browser request comes from this GUI's own origin.

    ``Sec-Fetch-Site`` is authoritative when present (browsers set it and pages
    cannot forge it); otherwise the ``Origin`` header (or the ``Referer``'s
    origin) must name this host (``Host`` / ``X-Forwarded-Host``) or a
    configured trusted origin. Requests without any of them are refused.
    """
    fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site == "same-origin":
        return True
    if fetch_site in ("cross-site", "same-site"):
        return False
    raw_origin = headers.get("origin")
    if raw_origin is None:
        origin = origin_of(headers.get("referer"))
    else:
        origin = origin_of(raw_origin)  # "null" (opaque origin) -> None
    if origin is None:
        return False
    if origin in settings.trusted_origins:
        return True
    scheme = origin.split("://", 1)[0]
    candidate = _host_port(origin)
    return any(
        _strip_default_port(host, scheme) == candidate
        for host in request_hosts(headers)
    )


class CSRFMiddleware:
    """Reject cookie-authenticated unsafe requests that are not same-origin.

    Requests authenticated with the admin bearer token are exempt: browsers
    cannot add that header to a cross-site request without a CORS preflight,
    and CORS is not enabled.
    """

    def __init__(self, app: ASGIApp, *, settings: AuthSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = Headers(scope=scope)
            if not uses_bearer(headers, self.settings) and not origin_allowed(
                headers, self.settings
            ):
                logger.warning(
                    "Refused a cross-origin %s %s (Origin=%r)",
                    scope["method"],
                    scope.get("path"),
                    headers.get("origin"),
                )
                response = JSONResponse(
                    {"detail": "Richiesta rifiutata: origine non consentita (CSRF)"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def external_base_url(conn: HTTPConnection) -> str:
    """Public base URL of the GUI, honouring reverse-proxy headers.

    Uses ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` / ``X-Forwarded-Prefix``
    (or RFC 7239 ``Forwarded``) when present and well-formed, else the request
    URL. A spoofed value only produces a redirect URI the IdP refuses.
    """
    headers = conn.headers
    forwarded = _parse_forwarded(headers.get("forwarded"))
    scheme = (
        _first(headers.get("x-forwarded-proto")) or forwarded.get("proto") or ""
    ).lower()
    if scheme not in ("http", "https"):
        scheme = conn.url.scheme if conn.url.scheme in ("http", "https") else "http"
    host = (
        _first(headers.get("x-forwarded-host"))
        or forwarded.get("host")
        or headers.get("host")
        or conn.url.netloc
    )
    if not host or not _HOST_RE.match(host):
        host = conn.url.netloc
    host = _strip_default_port(host, scheme)
    prefix = _first(headers.get("x-forwarded-prefix")) or ""
    prefix = prefix.rstrip("/")
    if prefix and not _PREFIX_RE.match(prefix):
        prefix = ""
    return f"{scheme}://{host}{prefix}"


def _parse_forwarded(value: str | None) -> dict[str, str]:
    first = _first(value)
    if not first:
        return {}
    result: dict[str, str] = {}
    for part in first.split(";"):
        key, sep, item = part.partition("=")
        if sep:
            result[key.strip().lower()] = item.strip().strip('"')
    return result
