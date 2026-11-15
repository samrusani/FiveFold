"""Action identity: the normal form of a write's business arguments and its key (0001).

`action_key = SHA-256(canonical_json({task_id, intent_id, operation, target, args}))`
(0001, item 5). Canonical JSON has its keys sorted, `,` and `:` as separators with no
spaces, non-ASCII characters as they are, and only text and integer values (item 6).
An id that does not match the id format is refused, never rewritten; only currency
codes are uppercased (item 7).

Record 0001 gives `S-1`, `W-100` and `CC-OPS` as examples without writing a format
down. Until one is, ids use the simulator's `ID_PATTERN` (`fivefold.domain.rules`),
which refuses only what cannot be an id at all: empty text, whitespace and `/`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Final

from fivefold.domain.rules import BUSINESS_ARGS, CURRENCY_PATTERN, ID_PATTERN, Operation

INTEGER_ARGS: Final = frozenset({"quantity", "unit_price_minor"})
"""Business arguments that are integers: quantities, and money in minor units (item 6)."""

_MINIMUM: Final[Mapping[str, int]] = {"quantity": 1, "unit_price_minor": 0}
"""The smallest value of each integer argument, as in the simulator's API."""

_ID: Final = re.compile(ID_PATTERN)
_CURRENCY: Final = re.compile(CURRENCY_PATTERN)


class InvalidArgs(ValueError):
    """A write's business arguments break the schema (refusal `invalid_args`, 0001)."""


def operation_of(name: str) -> Operation:
    """The write operation a tool name stands for, or `InvalidArgs`."""
    try:
        return Operation(name)
    except ValueError:
        raise InvalidArgs(f"unknown operation {name!r}") from None


def normalize(operation: Operation, args: Mapping[str, object]) -> dict[str, str | int]:
    """The business arguments of a write in normal form, or `InvalidArgs`.

    Fields outside the operation's business arguments, such as `based_on` and `note`,
    are left out: they are volatile and not part of the key (0001, item 7, table).
    """
    normal: dict[str, str | int] = {}
    for name in BUSINESS_ARGS[operation]:
        if name not in args:
            raise InvalidArgs(f"{name} is required")
        value = args[name]
        if name in INTEGER_ARGS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidArgs(f"{name} must be an integer")
            if value < _MINIMUM[name]:
                raise InvalidArgs(f"{name} must be at least {_MINIMUM[name]}")
            normal[name] = value
            continue
        if not isinstance(value, str):
            raise InvalidArgs(f"{name} must be text")
        if name == "currency":
            value = value.upper()
            if _CURRENCY.fullmatch(value) is None:
                raise InvalidArgs(f"currency {value!r} is not an ISO 4217 code")
        elif _ID.fullmatch(value) is None:
            raise InvalidArgs(f"{name} {value!r} is not an id")
        normal[name] = value
    return normal


def target_of(operation: Operation, args: Mapping[str, str | int]) -> str:
    """The resource a write acts on (0001, item 5)."""
    if operation is Operation.SUBMIT_PURCHASE_ORDER:
        return f"supplier/{args['supplier_id']}"
    return f"inventory/{args['warehouse_id']}/{args['sku']}"


def canonical_json(value: object) -> str:
    """JSON with sorted keys, no spaces, non-ASCII as is, and only text and integers."""
    _check_canonical(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _check_canonical(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidArgs("canonical JSON keys are text")
            _check_canonical(item)
    elif isinstance(value, list):
        for item in value:
            _check_canonical(item)
    elif isinstance(value, bool) or not isinstance(value, str | int):
        raise InvalidArgs(f"canonical JSON holds text and integers, not {type(value).__name__}")


def action_key(
    task_id: str, intent_id: str, operation: Operation, args: Mapping[str, str | int]
) -> str:
    """The action key of a write whose business arguments are already normal (0001)."""
    payload = {
        "task_id": task_id,
        "intent_id": intent_id,
        "operation": str(operation),
        "target": target_of(operation, args),
        "args": dict(args),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
