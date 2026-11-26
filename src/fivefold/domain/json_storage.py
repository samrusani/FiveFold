"""Validation shared by the API and the JSON records it writes."""

import json


def json_integer(value: int) -> int:
    """Refuse integers the process cannot serialize to its JSON messages or records."""
    try:
        str(value)
    except ValueError:
        raise ValueError("an integer is too large for the run's JSON records") from None
    return value


def purchase_amount(quantity: int, unit_price: int) -> int:
    """The derived amount must fit the same JSON records as the order terms."""
    return json_integer(quantity * unit_price)


def encodable_json(value: object) -> bool:
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        return False
    return True
