"""Exercise record 0023 against an operator's disposable local AliceMemory instance."""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from fivefold.domain.api import FactRead
from fivefold.domain.memory import Memory
from fivefold.domain.rules import memory_key
from fivefold.domain.scenario import MemoryFact
from fivefold.recorder.cassette import canonical_json

FLAG = "FIVEFOLD_ALICEMEMORY"
URL = "FIVEFOLD_ALICEMEMORY_URL"
KEY = "FIVEFOLD_ALICEMEMORY_KEY"
USER_ID = "FIVEFOLD_ALICEMEMORY_USER_ID"
MEMORIES = "/v0/vnext/memories"


class ExerciseError(ValueError):
    """An error safe to report without repeating a credential or response body."""


@dataclass(frozen=True)
class _Settings:
    url: str
    key: str = field(repr=False)
    user_id: UUID

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> _Settings:
        if environ.get(FLAG) != "1":
            raise ExerciseError(f"set {FLAG}=1 to run the local exercise")
        missing = [name for name in (URL, KEY, USER_ID) if not environ.get(name)]
        if missing:
            raise ExerciseError(f"set {', '.join(missing)} for the disposable instance")
        url, key = environ[URL], environ[KEY]
        try:
            parsed = urlsplit(url)
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError:
            raise ExerciseError(f"{URL} needs an HTTP URL on a literal loopback address") from None
        if (
            not address.is_loopback
            or parsed.scheme not in ("http", "https")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or port == 0
        ):
            raise ExerciseError(f"{URL} needs an HTTP URL on a literal loopback address")
        if not key.isascii() or not key.isprintable() or any(char.isspace() for char in key):
            raise ExerciseError(f"{KEY} must be a bearer key")
        try:
            user_id = UUID(environ[USER_ID])
        except ValueError:
            raise ExerciseError(f"{USER_ID} must be the disposable user's UUID") from None
        host = f"[{address}]" if address.version == 6 else str(address)
        authority = host if port is None else f"{host}:{port}"
        return cls(f"{parsed.scheme}://{authority}", key, user_id)


class _Row(BaseModel):
    id: UUID
    user_id: UUID
    canonical_text: str


class _Commit(BaseModel):
    status: Literal["committed"]
    memory: _Row


class _Revision(BaseModel):
    memory_id: UUID
    revision_number: int
    revision_type: Literal["created", "corrected"]
    text_before: str | None
    text_after: str


class _Audit(BaseModel):
    memory: _Row
    revisions: list[_Revision]


@dataclass
class _Entry:
    id: UUID
    versions: list[MemoryFact]


class AliceMemory:
    """Map exact AliceMemory reads and corrections to the FiveFold memory interface."""

    def __init__(self, client: httpx.Client, user_id: UUID) -> None:
        self._client = client
        self._user = user_id
        self._entries: dict[str, _Entry] = {}

    def _request[Response: BaseModel](
        self, model: type[Response], method: str, path: str, *, body: dict[str, str] | None = None
    ) -> Response:
        query = {"user_id": str(self._user)} if method == "GET" else None
        try:
            response = self._client.request(method, path, params=query, json=body)
        except httpx.RequestError:
            raise ExerciseError(f"{method} {path} could not reach the local instance") from None
        if not response.is_success:
            raise ExerciseError(f"{method} {path} returned HTTP {response.status_code}")
        try:
            return model.model_validate_json(response.content)
        except ValidationError:
            raise ExerciseError(
                f"{method} {path} returned a refusal or incomplete evidence"
            ) from None

    def _entry(self, key: str) -> _Entry:
        if key not in self._entries:
            raise ExerciseError("the exercise must seed the exact memory key before using it")
        return self._entries[key]

    def _check_row(self, row: _Row, expected: MemoryFact, memory_id: UUID | None = None) -> None:
        if row.user_id != self._user or (memory_id is not None and row.id != memory_id):
            raise ExerciseError("AliceMemory returned a memory with a different owner or id")
        if MemoryFact.model_validate_json(row.canonical_text) != expected:
            raise ExerciseError("AliceMemory returned different memory values")

    def seed(self, key: str, value: MemoryFact) -> None:
        result = self._request(
            _Commit,
            "POST",
            f"{MEMORIES}/commit",
            body={
                "user_id": str(self._user),
                "title": "FiveFold synthetic ordering channel",
                "canonical_text": canonical_json(value.model_dump(mode="json")),
                "idempotency_key": f"fivefold-f8-{self._user}-{key}",
            },
        )
        self._check_row(result.memory, value)
        self._entries[key] = _Entry(result.memory.id, [value])

    def read(self, key: str, *, caller: str, vt: int) -> FactRead:
        entry = self._entry(key)
        result = self._request(_Audit, "GET", f"{MEMORIES}/{entry.id}/audit")
        self._check_row(result.memory, entry.versions[-1], entry.id)
        if len(result.revisions) != len(entry.versions):
            raise ExerciseError("AliceMemory returned an incomplete revision history")
        for index, (revision, expected) in enumerate(
            zip(result.revisions, entry.versions, strict=True)
        ):
            if (
                revision.memory_id != entry.id
                or revision.revision_number != index + 1
                or revision.revision_type != ("created" if index == 0 else "corrected")
                or MemoryFact.model_validate_json(revision.text_after) != expected
            ):
                raise ExerciseError("AliceMemory returned a different revision")
            if index > 0 and (
                revision.text_before is None
                or MemoryFact.model_validate_json(revision.text_before) != entry.versions[index - 1]
            ):
                raise ExerciseError("AliceMemory did not retain the previous value")
        return self._fact(key, entry)

    def correct(self, key: str, changes: Mapping[str, str], *, actor: str, vt: int) -> FactRead:
        entry = self._entry(key)
        value = MemoryFact.model_validate({**entry.versions[-1].model_dump(), **changes})
        result = self._request(
            _Commit,
            "POST",
            f"{MEMORIES}/correct",
            body={
                "user_id": str(self._user),
                "memory_id": str(entry.id),
                "canonical_text": canonical_json(value.model_dump(mode="json")),
                "reason": "FiveFold synthetic ordering-channel correction",
            },
        )
        self._check_row(result.memory, value, entry.id)
        entry.versions.append(value)
        return self._fact(key, entry)

    @staticmethod
    def _fact(key: str, entry: _Entry) -> FactRead:
        return FactRead(key=key, version=len(entry.versions), data=entry.versions[-1].model_dump())


def main(*, transport: httpx.BaseTransport | None = None) -> int:
    try:
        settings = _Settings.from_env(os.environ)
        with httpx.Client(
            base_url=settings.url,
            headers={"Authorization": f"Bearer {settings.key}"},
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=10.0,
        ) as client:
            adapter = AliceMemory(client, settings.user_id)
            key = memory_key("S-1", "W-100")
            adapter.seed(key, MemoryFact(order_channel="edi", notes="FiveFold synthetic memory."))
            memory: Memory = adapter
            original = memory.read(key, caller="exercise", vt=0)
            memory.correct(key, {"order_channel": "portal"}, actor="exercise", vt=0)
            corrected = memory.read(key, caller="exercise", vt=0)
    except ExerciseError as error:
        print(f"AliceMemory exercise failed. {error}", file=sys.stderr)
        return 1
    except ValidationError:
        print("AliceMemory exercise failed. Invalid memory values.", file=sys.stderr)
        return 1
    # The adapter signals every refusal as an error, so these are FactRead results.
    assert isinstance(original, FactRead) and isinstance(corrected, FactRead)
    print(
        f"AliceMemory exercise passed for {key}. Version {original.version} edi"
        f" to version {corrected.version} portal. Previous value verified in the audit history."
    )
    return 0


if __name__ == "__main__":
    argparse.ArgumentParser(
        prog="python -m fivefold.harness.alicememory", description=__doc__
    ).parse_args()
    raise SystemExit(main())
