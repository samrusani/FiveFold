"""The gateway's basis check, with the dependency rules of record 0004.

Before an approval request and before every send, the dispatcher reads the action's
dependencies from the simulator and checks them here (0003, I3). The rules are the
shared ones in `fivefold.domain.rules`, which the simulator's C3 check and the evaluator
also use, so the gateway judges freshness exactly as property 3 does.

The cited value of each dependency comes from the gateway's own read records: the basis
names versions the gateway served, so it holds their values (0001, item 13). The value
now is what the dispatcher just read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fivefold.domain.rules import (
    FactValue,
    Operation,
    RuleKind,
    check_basis,
    dependencies,
)
from fivefold.gateway.destination import FactNow
from fivefold.gateway.records import Action, BasisCheck, StaleFact, frozen
from fivefold.gateway.store import ActionStore


class RuleFreshness:
    """Checks an action's basis against facts now, for the dispatcher."""

    def __init__(self, store: ActionStore) -> None:
        self._store = store

    def keys(self, action: Action) -> Sequence[str]:
        """The fact keys of the action's dependencies."""
        return [d.key for d in dependencies(Operation(action.operation), action.args)]

    def check(self, action: Action, now: Mapping[str, FactNow], *, full: bool) -> BasisCheck:
        """The basis check; `full` adds the sufficient rules to the exact ones (I3)."""
        history = _History(self._store, now)
        results = check_basis(
            Operation(action.operation),
            action.args,
            action.basis,
            history,
            exact_only=not full,
        )
        exact = [r for r in results if r.dependency.kind is RuleKind.EXACT]
        sufficient = [r for r in results if r.dependency.kind is RuleKind.SUFFICIENT]
        stale = tuple(
            StaleFact(r.dependency.key, r.cited_version, r.current_version, r.changed_fields)
            for r in results
            if not r.passed and r.cited_version is not None and r.current_version is not None
        )
        return BasisCheck(
            exact_ok=all(r.passed for r in exact),
            sufficient_ok=all(r.passed for r in sufficient) if full else None,
            versions_seen=frozen({key: fact.version for key, fact in now.items()}),
            stale=stale,
        )


class _History:
    """The facts as the gateway knows them: cited values from its reads, values now."""

    def __init__(self, store: ActionStore, now: Mapping[str, FactNow]) -> None:
        self._store = store
        self._now = now

    def value_at(self, key: str, version: int) -> FactValue | None:
        fact = self._now.get(key)
        if fact is not None and fact.version == version:
            return dict(fact.value)
        read = self._store.read_of(key, version)
        return None if read is None else dict(read.data)

    def current(self, key: str) -> tuple[int, FactValue] | None:
        fact = self._now.get(key)
        return None if fact is None else (fact.version, dict(fact.value))

    def was_served(self, key: str, version: int) -> bool:
        return self._store.read_of(key, version) is not None
