"""User persistence for Google authentication.

Only call this module after verifying the Google credential. Token verification
and the HTTP contract must be implemented once the team's auth spec is available.
The caller owns the transaction: these functions never commit or roll it back.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domains.auth.models import User


@dataclass(frozen=True)
class GoogleIdentity:
    """Profile obtained from a verified Google credential, never a request body."""

    sub: str
    email: str
    name: str
    profile_image_url: str | None = None


def get_or_create_google_user(db: Session, identity: GoogleIdentity) -> User:
    """Find by Google subject or create a user without changing onboarding state.

    Email is not an identity key. Existing profile fields are left as stored until
    the team specifies a profile synchronization policy. A savepoint recovers
    concurrent first logins without rolling back the caller's transaction.
    SQLAlchemy flushes pending work when opening a savepoint; callers should use
    a clean auth transaction (get_db provides a fresh session per request).
    """
    query = select(User).where(User.google_sub == identity.sub)
    user = db.scalar(query)
    if user is not None:
        return user

    try:
        with db.begin_nested():
            user = User(
                google_sub=identity.sub,
                email=identity.email,
                name=identity.name,
                profile_image_url=identity.profile_image_url,
            )
            db.add(user)
            db.flush()
    except IntegrityError:
        # Another login may have inserted this subject between SELECT and INSERT.
        # Other constraint failures must still reach the caller.
        user = db.scalar(query)
        if user is None:
            raise
    return user


def get_user_by_id(db: Session, user_id: int) -> User | None:
    """Look up a verified credential's user ID; this does not authenticate it."""
    return db.get(User, user_id)


def get_user_by_google_sub(db: Session, sub: str) -> User | None:
    return db.scalar(select(User).where(User.google_sub == sub))


def complete_signup(db: Session, identity: GoogleIdentity, marketing: bool) -> User:
    from datetime import datetime, timezone
    user = get_or_create_google_user(db, identity)
    user = db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if user.terms_agreed_at is not None:
        raise ValueError("SIGNUP_ALREADY_COMPLETED")
    user.terms_agreed_at = datetime.now(timezone.utc)
    user.marketing_agreed = marketing
    db.flush()
    return user


def complete_onboarding(db: Session, user: User) -> User:
    """Mark the authenticated user complete; the caller owns the transaction."""
    user.onboarding_completed = True
    db.flush()
    return user
