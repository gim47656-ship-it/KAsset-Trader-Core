"""Security utilities for authentication."""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import settings

# Parity with the passlib CryptContext this module used before: bcrypt at 12
# rounds, UTF-8 input, passwords over 4096 bytes and NUL bytes rejected with
# ValueError, and anything past bcrypt's 72 bytes silently ignored.
_BCRYPT_ROUNDS = 12
_MAX_PASSWORD_BYTES = 4096
# Fixed cost-12 hash of a non-account string, used to spend the same bcrypt
# work when no stored hash exists (passlib's CryptContext did this for None).
_DUMMY_HASH = b"$2b$12$h9GBK/RajMn30qUm8WNITOcbQdNHLnBSciF6m9V47OLpBpLQ7AzIS"


def _encode_password(password: str) -> bytes:
    secret = password.encode("utf-8")
    if len(secret) > _MAX_PASSWORD_BYTES:
        raise ValueError("password exceeds maximum allowed size")
    if b"\x00" in secret:
        raise ValueError("bcrypt does not allow NULL bytes in password")
    return secret


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    """
    Verify a plain password against a hashed password.

    Args:
        plain_password: Plain text password
        hashed_password: Hashed password from database

    Returns:
        True if password matches, False otherwise
    """
    secret = _encode_password(plain_password)
    if hashed_password is None:
        # Spend the same bcrypt work as a real check so a missing hash does
        # not answer faster than a wrong password.
        bcrypt.checkpw(secret, _DUMMY_HASH)
        return False
    if hashed_password.startswith("$2x$"):
        raise ValueError("crypt_blowfish's buggy '2x' hashes are not supported")
    return bcrypt.checkpw(secret, hashed_password.encode("utf-8"))


def get_password_hash(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: Plain text password

    Returns:
        Hashed password
    """
    return bcrypt.hashpw(
        _encode_password(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    ).decode("ascii")


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Create a JWT access token.

    Args:
        data: Data to encode in the token (typically {"sub": username})
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt


def create_refresh_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Create a JWT refresh token.

    Args:
        data: Data to encode in the token (typically {"sub": username})
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt
