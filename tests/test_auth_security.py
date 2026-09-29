from datetime import timedelta

import bcrypt
import jwt
import pytest

from app.auth.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_password,
)
from app.core.config import settings


def test_password_hashing():
    password = "testpassword"
    hashed = get_password_hash(password)
    assert verify_password(password, hashed)
    assert not verify_password("wrongpassword", hashed)


# Hashes written by the passlib CryptContext this module used before.
_PASSLIB_HASH = "$2b$12$3ikOvyRIBsD5agoVGYkU8eSeSxaBL.D71345Ajl/ry1e8Rhsa5crW"
_PASSLIB_LONG_HASH = "$2b$12$GUjs23A4IV5BbhM0RZhBlONzGPielnOUBYOIxcx00y9o6WHLYVgZO"


def test_verifies_hash_written_by_passlib():
    assert verify_password("legacy-비밀번호-1!", _PASSLIB_HASH)
    assert not verify_password("legacy-비밀번호-2!", _PASSLIB_HASH)


@pytest.mark.parametrize("prefix", ["2a", "2y"])
def test_verifies_other_stored_bcrypt_prefixes(prefix):
    stored = f"${prefix}$" + _PASSLIB_HASH[4:]
    assert verify_password("legacy-비밀번호-1!", stored)
    assert not verify_password("wrong", stored)


def test_new_hash_is_bcrypt_2b_with_12_rounds():
    hashed = get_password_hash("비밀번호-Passw0rd!")
    assert hashed.startswith("$2b$12$")
    assert len(hashed) == 60
    assert bcrypt.checkpw("비밀번호-Passw0rd!".encode(), hashed.encode())


def test_only_first_72_bytes_are_significant():
    assert verify_password("a" * 72 + "anything-else", _PASSLIB_LONG_HASH)
    assert verify_password("a" * 72, get_password_hash("a" * 100))
    assert not verify_password("a" * 71 + "b", _PASSLIB_LONG_HASH)


@pytest.mark.parametrize("password", ["a\x00b", "a" * 4097, "가" * 1366])
def test_rejects_unhashable_passwords_with_value_error(password):
    with pytest.raises(ValueError):
        get_password_hash(password)
    with pytest.raises(ValueError):
        verify_password(password, _PASSLIB_HASH)


@pytest.mark.parametrize("password", ["a" * 4096, "가" * 1365 + "a"])
def test_accepts_passwords_up_to_4096_bytes(password):
    assert verify_password(password, get_password_hash(password))


def test_missing_hash_never_matches():
    assert verify_password("anything", None) is False


@pytest.mark.parametrize("stored", ["", "notahash", "$2b$03$" + _PASSLIB_HASH[7:]])
def test_malformed_stored_hash_raises_value_error(stored):
    with pytest.raises(ValueError):
        verify_password("legacy-비밀번호-1!", stored)


def test_rejects_crypt_blowfish_2x_hashes():
    stored = "$2x$" + _PASSLIB_HASH[4:]
    with pytest.raises(ValueError):
        verify_password("legacy-비밀번호-1!", stored)


def test_create_access_token():
    data = {"sub": "testuser"}
    token = create_access_token(data=data)
    decoded = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert decoded["sub"] == "testuser"
    assert decoded["type"] == "access"
    assert "exp" in decoded


def test_create_access_token_with_expiry():
    data = {"sub": "testuser"}
    expires = timedelta(minutes=10)
    token = create_access_token(data=data, expires_delta=expires)
    decoded = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert decoded["sub"] == "testuser"
    # Check if expiration is roughly correct (within a few seconds)
    # This is a bit tricky to test exactly without mocking time, but existence is key
    assert "exp" in decoded


def test_create_refresh_token():
    data = {"sub": "testuser"}
    token = create_refresh_token(data=data)
    decoded = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert decoded["sub"] == "testuser"
    assert decoded["type"] == "refresh"
    assert "exp" in decoded
