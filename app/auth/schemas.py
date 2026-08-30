"""Pydantic schemas for authentication."""

import string

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.trading import UserRole


def _validate_password_strength(value: str) -> str:
    if len(value) < 8:
        raise ValueError("비밀번호는 최소 8자 이상이어야 합니다.")
    if not any(character.isupper() for character in value):
        raise ValueError("비밀번호에 대문자가 최소 1개 이상 포함되어야 합니다.")
    if not any(character.isdigit() for character in value):
        raise ValueError("비밀번호에 숫자가 최소 1개 이상 포함되어야 합니다.")
    if not any(character in string.punctuation for character in value):
        raise ValueError("비밀번호에 특수문자가 최소 1개 이상 포함되어야 합니다.")
    return value


class Token(BaseModel):
    """Token response schema."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    """Data extracted from JWT token."""

    username: str | None = None


class UserCreate(BaseModel):
    """Schema for creating a new user."""

    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        """비밀번호 강도 검증: 최소 8자, 대문자, 숫자, 특수문자 포함."""
        return _validate_password_strength(value)


class PasswordResetConfirm(BaseModel):
    """Validate a new password without requiring registration identity fields."""

    password: str = Field(..., min_length=8)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        return _validate_password_strength(value)


class UserInDB(BaseModel):
    """User schema for database representation."""

    id: int
    email: str
    username: str
    is_active: bool

    class Config:
        from_attributes = True


class UserResponse(BaseModel):
    """User response schema (excludes sensitive data)."""

    id: int
    email: str
    username: str
    is_active: bool
    role: UserRole

    class Config:
        from_attributes = True


class RefreshTokenRequest(BaseModel):
    """Schema for refresh token request."""

    refresh_token: str
