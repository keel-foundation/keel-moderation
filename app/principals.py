from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["creator", "moderator", "appeals", "operator"]

ALLOWED_ROLES = frozenset({"creator", "moderator", "appeals", "operator"})
MAX_CHANNEL_ID_LENGTH = 80


@dataclass(frozen=True)
class Principal:
    id: str
    role: Role
    channel_ids: tuple[str, ...] = ()


def actor_payload(principal: Principal) -> dict:
    return {"id": principal.id, "role": principal.role}


def valid_channel_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_CHANNEL_ID_LENGTH
        and value.startswith("ch_")
        and value.isascii()
        and all(character.isalnum() or character == "_" for character in value)
    )
