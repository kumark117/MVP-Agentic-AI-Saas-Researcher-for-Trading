"""Argon2id password hashing (D013). Parameters are configuration so tests can use cheap ones."""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


@dataclass(frozen=True, slots=True)
class Argon2Params:
    # Defaults follow the RFC 9106 "low memory" profile (64 MiB, t=3, p=4), which takes
    # roughly 50-100 ms on the dev machine. Calibrate per deployment host.
    time_cost: int = 3
    memory_kib: int = 64 * 1024
    parallelism: int = 4


class PasswordService:
    def __init__(self, params: Argon2Params | None = None) -> None:
        p = params or Argon2Params()
        self._hasher = PasswordHasher(
            time_cost=p.time_cost, memory_cost=p.memory_kib, parallelism=p.parallelism
        )
        # Verified against when the account does not exist, so timing does not reveal it.
        self._dummy_hash = self._hasher.hash("dummy-password-for-timing-equalisation")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash or self._dummy_hash, password) and bool(
                password_hash
            )
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)
