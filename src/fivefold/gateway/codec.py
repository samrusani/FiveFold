"""Gateway records to and from JSON, for the `jsonb` columns of the `gw` schema.

Decoding gives back records equal to the ones encoded, so the Postgres store's snapshots
compare equal to the in-memory store's. JSON has no tuples and no record types, so the
decoder restores them: lists become tuples, objects with exactly a stale fact's keys
become `StaleFact`, and other objects become read-only mappings. Enum values decode as
plain strings, except in event details, where the keys that hold enums (`trigger` and
`approval`) get their enum back, so decoded details are equal and of the same types.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import cast

from fivefold.domain.contracts import Contract
from fivefold.gateway.records import (
    ActionResult,
    ApprovalStatus,
    BasisCheck,
    CheckRecord,
    Claim,
    ClaimConflict,
    ClaimStatus,
    Reconcile,
    RecordedAction,
    Refusal,
    RefusalReason,
    ReportAccepted,
    StaleFact,
    frozen,
)
from fivefold.gateway.state_machine import (
    ActionState,
    CheckKind,
    EscalationTrigger,
    Reason,
    ReconcileHow,
)

JSON = object
"""A value `json.loads` can return."""

_STALE_KEYS = frozenset({"key", "cited_version", "current_version", "changed_fields"})


class CodecError(ValueError):
    """Stored JSON does not have the shape a record needs."""


# Generic values ------------------------------------------------------------------------


def to_json(value: object) -> JSON:
    """Encode a detail value, an argument or a basis as JSON-compatible data."""
    if isinstance(value, StaleFact):
        return {
            "key": value.key,
            "cited_version": value.cited_version,
            "current_version": value.current_version,
            "changed_fields": list(value.changed_fields),
        }
    if isinstance(value, StrEnum):
        return str(value)
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [to_json(item) for item in value]
    raise CodecError(f"cannot store {type(value).__name__} as JSON")


def from_json(data: JSON) -> object:
    """Decode what `to_json` wrote: tuples, stale facts and read-only mappings."""
    if isinstance(data, list):
        return tuple(from_json(item) for item in data)
    if isinstance(data, dict):
        if data.keys() == _STALE_KEYS:
            return _stale(data)
        return frozen({str(key): from_json(item) for key, item in data.items()})
    if data is None or isinstance(data, str | int | bool):
        return data
    raise CodecError(f"unexpected JSON value {data!r}")


_DETAIL_ENUMS: Mapping[str, type[StrEnum]] = {
    "trigger": EscalationTrigger,
    "approval": ApprovalStatus,
}
"""Event detail keys whose values are enum members."""


def mapping_from_json(data: JSON) -> Mapping[str, object]:
    """Decode a JSON object, such as an event's details, restoring enum members."""
    decoded = from_json(data)
    if not isinstance(decoded, Mapping):
        raise CodecError(f"expected a JSON object, found {data!r}")
    restored = dict(decoded)
    for key, enum in _DETAIL_ENUMS.items():
        value = restored.get(key)
        if isinstance(value, str):
            restored[key] = enum(value)
    return frozen(restored)


def args_from_json(data: JSON) -> Mapping[str, str | int]:
    """Decode an action's business arguments."""
    values = _object(data)
    for key, item in values.items():
        if not isinstance(item, str | int) or isinstance(item, bool):
            raise CodecError(f"argument {key} must be text or an integer")
    return frozen(cast(dict[str, str | int], values))


def versions_from_json(data: JSON) -> Mapping[str, int]:
    """Decode a basis: fact key to version."""
    values = _object(data)
    for key, item in values.items():
        if not isinstance(item, int) or isinstance(item, bool):
            raise CodecError(f"version of {key} must be an integer")
    return frozen(cast(dict[str, int], values))


# Checks ----------------------------------------------------------------------------------


def basis_to_json(basis: BasisCheck | None) -> JSON:
    """Encode the basis part of a check."""
    if basis is None:
        return None
    return {
        "exact_ok": basis.exact_ok,
        "sufficient_ok": basis.sufficient_ok,
        "versions_seen": to_json(basis.versions_seen),
        "stale": to_json(basis.stale),
    }


def check_from_columns(
    kind: str,
    approval: str | None,
    basis: JSON,
    seen_seq: int,
    check_vt: int,
    approval_expires_at: int | None,
) -> CheckRecord:
    """Rebuild a check record from an attempt row's check columns."""
    decoded: BasisCheck | None = None
    if basis is not None:
        values = _object(basis)
        sufficient = values.get("sufficient_ok")
        decoded = BasisCheck(
            exact_ok=_bool(values.get("exact_ok")),
            sufficient_ok=None if sufficient is None else _bool(sufficient),
            versions_seen=versions_from_json(values.get("versions_seen", {})),
            stale=tuple(_stale(item) for item in _list(values.get("stale", []))),
        )
    status = None if approval is None else ApprovalStatus(approval)
    return CheckRecord(CheckKind(kind), status, decoded, seen_seq, check_vt, approval_expires_at)


# Responses -------------------------------------------------------------------------------


def result_to_json(result: ActionResult | Refusal | ReportAccepted) -> JSON:
    """Encode a logged response in the shapes the agent sees (0003)."""
    if isinstance(result, ActionResult):
        reconcile = result.reconcile
        return {
            "result": "action",
            "action_id": result.action_id,
            "intent_id": result.intent_id,
            "state": str(result.state),
            "effect_id": result.effect_id,
            "reason": str(result.reason),
            "stale": to_json(result.stale),
            "reconcile": None
            if reconcile is None
            else {
                "contract": str(reconcile.contract),
                "how": str(reconcile.how),
                "escalation_id": reconcile.escalation_id,
                "what": reconcile.what,
            },
        }
    if isinstance(result, Refusal):
        return {
            "result": "refused",
            "reason": str(result.reason),
            "action_id": result.action_id,
            "state": None if result.state is None else str(result.state),
            "effect_id": result.effect_id,
            "args": None if result.args is None else to_json(result.args),
            "conflicts": [
                {
                    "intent_id": conflict.intent_id,
                    "claimed": str(conflict.claimed),
                    "claimed_effect_ids": list(conflict.claimed_effect_ids),
                    "recorded": [
                        {
                            "action_id": recorded.action_id,
                            "state": str(recorded.state),
                            "effect_ids": list(recorded.effect_ids),
                        }
                        for recorded in conflict.recorded
                    ],
                }
                for conflict in result.conflicts
            ],
        }
    return {"result": "accepted", "claims": [_claim_to_json(claim) for claim in result.claims]}


def result_from_json(data: JSON) -> ActionResult | Refusal | ReportAccepted:
    """Decode a logged response."""
    values = _object(data)
    kind = values.get("result")
    if kind == "action":
        reconcile_data = values.get("reconcile")
        reconcile = None
        if reconcile_data is not None:
            r = _object(reconcile_data)
            reconcile = Reconcile(
                Contract(_str(r.get("contract"))),
                ReconcileHow(_str(r.get("how"))),
                _optional_str(r.get("escalation_id")),
                _str(r.get("what")),
            )
        return ActionResult(
            _str(values.get("action_id")),
            _str(values.get("intent_id")),
            ActionState(_str(values.get("state"))),
            _optional_str(values.get("effect_id")),
            Reason(_str(values.get("reason"))),
            tuple(_stale(item) for item in _list(values.get("stale", []))),
            reconcile,
        )
    if kind == "refused":
        state = values.get("state")
        args = values.get("args")
        return Refusal(
            RefusalReason(_str(values.get("reason"))),
            _optional_str(values.get("action_id")),
            None if state is None else ActionState(_str(state)),
            _optional_str(values.get("effect_id")),
            None if args is None else args_from_json(args),
            tuple(_conflict(item) for item in _list(values.get("conflicts", []))),
        )
    if kind == "accepted":
        return ReportAccepted(tuple(_claim(item) for item in _list(values.get("claims", []))))
    raise CodecError(f"unknown response kind {kind!r}")


def _claim_to_json(claim: Claim) -> JSON:
    return {
        "intent_id": claim.intent_id,
        "status": str(claim.status),
        "effect_ids": list(claim.effect_ids),
        "note": claim.note,
    }


def _claim(data: JSON) -> Claim:
    values = _object(data)
    return Claim(
        _str(values.get("intent_id")),
        ClaimStatus(_str(values.get("status"))),
        tuple(_str(item) for item in _list(values.get("effect_ids", []))),
        _str(values.get("note", "")),
    )


def _conflict(data: JSON) -> ClaimConflict:
    values = _object(data)
    recorded = tuple(
        RecordedAction(
            _str(item.get("action_id")),
            ActionState(_str(item.get("state"))),
            tuple(_str(e) for e in _list(item.get("effect_ids", []))),
        )
        for item in (_object(entry) for entry in _list(values.get("recorded", [])))
    )
    return ClaimConflict(
        _str(values.get("intent_id")),
        ClaimStatus(_str(values.get("claimed"))),
        tuple(_str(item) for item in _list(values.get("claimed_effect_ids", []))),
        recorded,
    )


# Checked access to decoded JSON ------------------------------------------------------------


def _stale(data: JSON) -> StaleFact:
    values = _object(data)
    return StaleFact(
        _str(values.get("key")),
        _int(values.get("cited_version")),
        _int(values.get("current_version")),
        tuple(_str(item) for item in _list(values.get("changed_fields", []))),
    )


def _object(data: JSON) -> dict[str, object]:
    if not isinstance(data, dict):
        raise CodecError(f"expected a JSON object, found {data!r}")
    return {str(key): item for key, item in data.items()}


def _list(data: JSON) -> Sequence[object]:
    if not isinstance(data, list | tuple):
        raise CodecError(f"expected a JSON array, found {data!r}")
    return data


def _str(data: JSON) -> str:
    if not isinstance(data, str):
        raise CodecError(f"expected text, found {data!r}")
    return data


def _optional_str(data: JSON) -> str | None:
    return None if data is None else _str(data)


def _int(data: JSON) -> int:
    if not isinstance(data, int) or isinstance(data, bool):
        raise CodecError(f"expected an integer, found {data!r}")
    return data


def _bool(data: JSON) -> bool:
    if not isinstance(data, bool):
        raise CodecError(f"expected true or false, found {data!r}")
    return data
