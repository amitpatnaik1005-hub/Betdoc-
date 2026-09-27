import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Bounded length: rejects empty credentials (encrypt_api_key raises on "") and oversized payloads
Credential = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]


class ExchangeAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange_name: Literal["Pinnacle", "Betfair"]
    # repr=False: plaintext credentials never appear in repr(), logs or tracebacks
    api_key: Credential = Field(repr=False)
    api_secret: Credential = Field(repr=False)


class ExchangeAccountRead(BaseModel):
    """Public view. Deliberately contains NO credential fields, encrypted or otherwise."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exchange_name: str
    is_active: bool
