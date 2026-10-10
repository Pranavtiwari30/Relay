"""Password authentication and server-side sessions.

Nothing here is home-made cryptography: passwords use Argon2id via
``argon2-cffi``; session and CSRF tokens come from ``secrets``; only a SHA-256
of the session token is stored, so a database leak does not yield usable cookies.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as token_source
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from relay_core.config import Settings
from relay_core.db.models import User, UserSession
from relay_core.db.session import SessionFactory, session_scope
from relay_core.errors import RateLimitedError, UnauthenticatedError, ValidationFailedError
from relay_core.services import ratelimit
from relay_core.services.audit import record_audit
from relay_core.services.event_state import Actor

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
LOGIN_WINDOW_SECONDS = 900
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)

_hasher = PasswordHasher()  # Argon2id with the library's current recommended parameters


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash(token_source.token_urlsafe(16))


def normalise_email(email: str) -> str:
    return email.strip().lower()


def check_password_policy(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValidationFailedError(
            f"password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters",
            code="weak_password",
        )


def hash_password(password: str) -> str:
    check_password_policy(password)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class LoginResult:
    user: User
    session_token: str
    csrf_token: str
    expires_at: datetime


def _check_login_rate(
    session_factory: SessionFactory,
    settings: Settings,
    email: str,
    client_ip: str | None,
    now: datetime,
) -> None:
    limit = settings.login_rate_limit_per_15_minutes
    keys = [(f"login:email:{hashlib.sha256(email.encode()).hexdigest()[:32]}", limit)]
    if client_ip:
        keys.append((f"login:ip:{client_ip[:64]}", limit * 5))
    for key, key_limit in keys:
        decision = ratelimit.hit(
            session_factory, key, limit=key_limit, window_seconds=LOGIN_WINDOW_SECONDS, now=now
        )
        if not decision.allowed:
            raise RateLimitedError(
                "too many sign-in attempts; try again later",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )


def login(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    email: str,
    password: str,
    user_agent: str | None,
    client_ip: str | None,
    now: datetime,
) -> LoginResult:
    email = normalise_email(email)
    # Counted before the password check so guessing is bounded per account and per address.
    _check_login_rate(session_factory, settings, email, client_ip, now)
    with session_scope(session_factory) as session:
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None or not user.is_active:
            # Spend the same work as a real check so timing does not reveal
            # whether the account exists.
            verify_password(_dummy_hash(), password)
            raise UnauthenticatedError("invalid email or password", code="invalid_credentials")
        if not verify_password(user.password_hash, password):
            raise UnauthenticatedError("invalid email or password", code="invalid_credentials")
        if _hasher.check_needs_rehash(user.password_hash):
            user.password_hash = _hasher.hash(password)
        token = token_source.token_urlsafe(32)
        csrf = token_source.token_urlsafe(32)
        expires = now + timedelta(hours=settings.session_ttl_hours)
        session.add(
            UserSession(
                id=uuid.uuid4(),
                user_id=user.id,
                token_hash=hash_token(token),
                csrf_token=csrf,
                created_at=now,
                last_seen_at=now,
                expires_at=expires,
                user_agent=(user_agent or "")[:300] or None,
            )
        )
        user.last_login_at = now
        return LoginResult(user=user, session_token=token, csrf_token=csrf, expires_at=expires)


def resolve_session(
    session: Session, token: str, *, now: datetime
) -> tuple[UserSession, User] | None:
    if not token or len(token) > 200:
        return None
    row = session.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(
            UserSession.token_hash == hash_token(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            User.is_active.is_(True),
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


def touch_session(
    session_factory: SessionFactory, user_session: UserSession, *, now: datetime
) -> None:
    """Record activity occasionally; not on every request, to keep reads cheap."""
    if now - user_session.last_seen_at < SESSION_TOUCH_INTERVAL:
        return
    with session_scope(session_factory) as session:
        session.execute(
            update(UserSession).where(UserSession.id == user_session.id).values(last_seen_at=now)
        )


def csrf_matches(user_session: UserSession, provided: str | None) -> bool:
    return bool(provided) and hmac.compare_digest(user_session.csrf_token, provided or "")


def logout(session: Session, user_session: UserSession, *, now: datetime) -> None:
    session.execute(
        update(UserSession).where(UserSession.id == user_session.id).values(revoked_at=now)
    )


def change_password(
    session: Session,
    *,
    user: User,
    current_session: UserSession,
    current_password: str,
    new_password: str,
    now: datetime,
) -> None:
    db_user = session.get_one(User, user.id)
    if not verify_password(db_user.password_hash, current_password):
        raise UnauthenticatedError("current password is incorrect", code="invalid_credentials")
    db_user.password_hash = hash_password(new_password)
    # Rotating a credential ends every other session that used the old one.
    session.execute(
        update(UserSession)
        .where(
            UserSession.user_id == user.id,
            UserSession.id != current_session.id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    record_audit(
        session,
        workspace_id=None,
        actor=Actor.user(user.id),
        action="user.password_change",
        entity_type="user",
        entity_id=user.id,
    )
