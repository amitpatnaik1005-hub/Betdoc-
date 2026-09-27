import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

BCRYPT_MAX_BYTES = 72

Username = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,  # "Admin" and "admin" are the same account; blocks look-alike impersonation
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9_.-]+$",
    ),
]


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # Rejects attempts to send role/is_active at signup

    username: Username
    password: str = Field(min_length=8)

    @field_validator("password")
    @classmethod
    def password_within_bcrypt_limit(cls, v: str) -> str:
        if len(v.encode("utf-8")) > BCRYPT_MAX_BYTES:
            raise ValueError(f"Password must not exceed {BCRYPT_MAX_BYTES} bytes")
        return v


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: str
    is_active: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str
