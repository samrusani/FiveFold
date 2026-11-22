"""The declaration shared by stdio and local Streamable HTTP fixtures."""

from __future__ import annotations

from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt, model_validator

NAMESPACE = "{{namespace}}"
OPERATION = "{{operation_id}}"
EFFECT = "{{expected_effect}}"
Label = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.:/-]{1,128}$")]
Milliseconds = Annotated[StrictInt, Field(gt=0)]


class Case(StrEnum):
    LOST_REPLY = "lost_reply"
    DUPLICATE = "duplicate_delivery"
    PRE_COMMIT = "timeout_before_commit"


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stdio(Declaration):
    kind: Literal["stdio"]
    command: Annotated[str, Field(min_length=1)]
    args: list[str] = Field(default_factory=list)


class StreamableHttp(Declaration):
    kind: Literal["streamable_http"]
    url: str

    @model_validator(mode="after")
    def require_loopback(self) -> Self:
        try:
            parts = urlsplit(self.url)
            allowed = (
                parts.scheme in ("http", "https")
                and parts.hostname is not None
                and ip_address(parts.hostname).is_loopback
                and parts.port != 0
                and parts.username is None
                and not parts.query
                and not parts.fragment
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise ValueError("use a loopback HTTP endpoint with a literal IP address")
        return self


class Tool(Declaration):
    name: Label
    arguments: dict[str, JsonValue]


class Hooks(Declaration):
    arm: Tool
    held: Tool
    abort: Tool


class Fixture(Declaration):
    version: Label
    transport: Annotated[Stdio | StreamableHttp, Field(discriminator="kind")]
    write: Tool
    probe: Tool
    expected_effect: JsonValue
    retry_identity: Literal["same_arguments", "key", "none"]
    key_argument: str | None = None
    supported_cases: set[Case]
    terminal_replies_settle: StrictBool
    evidence_wait_ms: Milliseconds
    caller_wait_ms: Milliseconds
    pre_commit: Hooks | None = None

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        if self.retry_identity == "key":
            if not self.key_argument or self.key_argument in self.write.arguments:
                raise ValueError("declare a free write argument for the retry key")
        elif self.key_argument is not None:
            raise ValueError("key_argument requires key retry identity")
        if Case.PRE_COMMIT in self.supported_cases and self.pre_commit is None:
            raise ValueError("timeout_before_commit requires fixture hooks")
        if self.caller_wait_ms >= self.evidence_wait_ms:
            raise ValueError("caller wait must fit inside the evidence deadline")
        tools = [self.write, self.probe]
        if self.pre_commit is not None:
            tools.extend((self.pre_commit.arm, self.pre_commit.held, self.pre_commit.abort))
        if any(not contains(tool.arguments, NAMESPACE) for tool in tools):
            raise ValueError("every tool must receive the case namespace")
        if not contains(self.probe.arguments, EFFECT):
            raise ValueError("the probe must receive the declared expected effect")
        return self


class Probe(Declaration):
    count: Annotated[StrictInt, Field(ge=0)]
    matches: StrictBool
    settled: StrictBool | None = None


def contains(value: JsonValue, token: str) -> bool:
    if isinstance(value, dict):
        return any(contains(entry, token) for entry in value.values())
    if isinstance(value, list):
        return any(contains(entry, token) for entry in value)
    return value == token


def render(value: JsonValue, values: dict[str, JsonValue]) -> JsonValue:
    if isinstance(value, dict):
        return {name: render(entry, values) for name, entry in value.items()}
    if isinstance(value, list):
        return [render(entry, values) for entry in value]
    if isinstance(value, str):
        return values.get(value, value)
    return value
