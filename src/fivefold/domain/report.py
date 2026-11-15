"""The final report's schema (0004, property 5; spec, Gateway).

```json
{"claims": [{"intent_id": "REQ-1", "status": "done", "effect_ids": ["PO-0001"], "note": "..."}]}
```

`status` is `done`, `not_done` or `unknown`. `effect_ids` is non-empty for `done` and
empty otherwise. A report has exactly one claim per task intent. The gateway refuses a
report that breaks this schema, so the agent can fix it and resubmit; the harness's
report sink marks it invalid; the evaluator counts the last valid report.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator

Text = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$", max_length=2_000)]
"""Text without a NUL character, which no store can keep."""


class ClaimStatus(StrEnum):
    """What a claim says about its intent (0004, property 5)."""

    DONE = "done"
    NOT_DONE = "not_done"
    UNKNOWN = "unknown"


class ClaimModel(BaseModel):
    """One claim about one task intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: Text
    status: ClaimStatus
    effect_ids: tuple[Text, ...] = ()
    note: Text = ""

    @model_validator(mode="after")
    def _effects_for_done(self) -> ClaimModel:
        if (self.status is ClaimStatus.DONE) != bool(self.effect_ids):
            raise ValueError("effect_ids must be non-empty for done and empty otherwise")
        return self


class FinalReport(BaseModel):
    """A final report: one claim per task intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claims: tuple[ClaimModel, ...]


class InvalidReport(ValueError):
    """A report that breaks the schema; `errors` says how, one line each."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_report(data: object, intents: Collection[str]) -> FinalReport:
    """The report in `data`, or `InvalidReport` listing everything wrong with it."""
    try:
        report = FinalReport.model_validate(data)
    except ValidationError as error:
        raise InvalidReport(validation_messages(error)) from None
    errors: list[str] = []
    claimed = [claim.intent_id for claim in report.claims]
    for intent_id in sorted(set(claimed)):
        if claimed.count(intent_id) > 1:
            errors.append(f"{intent_id} has more than one claim")
    for intent_id in sorted(set(claimed) - set(intents)):
        errors.append(f"{intent_id} is not an intent of the task")
    for intent_id in sorted(set(intents) - set(claimed)):
        errors.append(f"{intent_id} has no claim")
    if errors:
        raise InvalidReport(errors)
    return report


def validation_messages(error: ValidationError) -> list[str]:
    """One line per problem, naming the field: `claims.0.status: Input should be ...`."""
    lines = []
    for problem in error.errors(include_url=False):
        where = ".".join(str(part) for part in problem["loc"])
        lines.append(f"{where}: {problem['msg']}" if where else str(problem["msg"]))
    return lines
