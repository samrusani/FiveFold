# 0016: Simulator schema writer

Status: Accepted on 16 Nov 2026 by the maintainer
Date: 2026-11-16
Spec sections affected: Architecture (trust boundaries), Stack (database roles)

## Context

Record 0008 says `ff_sim` owns schema `sim`. The migrations create the schemas without an `AUTHORIZATION` clause, so the role running the migrations owns them. The simulator role has write privileges on its tables. The privilege tests check the component roles' access.

## Decision

This record amends 0008's simulator row. Among the component roles, only `ff_sim` writes schema `sim`. The migration role owns every schema.

The other component roles cannot write `sim`. The migration role retains its owner privileges. No database role, grant or schema changes.

## Alternatives considered

- Transfer ownership of `sim` to `ff_sim`. This changes the database rather than correcting the description.
- Leave 0008's wording as it is. It describes a schema owner the migrations do not set.

## Consequences

The spec describes the privilege boundary the database already enforces. `tests/pg/test_privileges.py` continues to check component privileges. Record 0008 stays as written and is amended by this record.
