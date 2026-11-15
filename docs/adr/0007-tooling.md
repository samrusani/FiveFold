# 0007: Tooling

Status: Accepted (2026-11-14)
Amended by: 0012 (2026-11-15): the Viewer row of the tooling table
Date: 2026-11-14
Spec sections affected: Stack, Repo layout, plan.md (T5 tickets)
Resolves M0 questions: 43, 44

## Context

The repository scaffold is built against this record. The choices should be mainstream, current and easy to verify. They should also make the hard rules in CLAUDE.md cheap to enforce: determinism, no shared transactions, and no invented APIs.

Versions on PyPI on 14 Nov 2026: mcp 2.3.0, langgraph 1.2.22, langgraph-prebuilt 1.1.2, langgraph-checkpoint-postgres 3.2.1, langchain-core 1.8.0, fastapi 0.162.7, pydantic 2.14.3, psycopg 3.3.9, hypothesis 6.170.15, pytest 9.2.0, ruff 0.17.5, mypy 2.4.1, hatchling 1.33.0. The scaffold pins exact versions in the lockfile.

## Decision

| Area | Choice |
| --- | --- |
| Python | 3.12, pinned in `.python-version`; `requires-python = ">=3.12,<3.13"` |
| Environments and locking | uv; `uv.lock` is committed; CI installs from the lockfile and fails if it is out of date |
| Build backend | hatchling, src layout |
| Package | one import package, `fivefold`, under `src/` |
| Lint and format | Ruff for both |
| Types | mypy in strict mode on `src/`, with the Pydantic mypy plugin (present in pydantic 2.14.3) |
| Database access | psycopg 3 with hand-written SQL; no ORM |
| Migrations | plain SQL files, forward-only, applied by a small runner in the repo |
| Tests | pytest and Hypothesis |
| Postgres for tests | the Compose `postgres` service locally; a `postgres:16` service container in CI |
| CI | GitHub Actions |
| Containers | Docker Compose with `postgres` (a pinned 16.x image) and the app image (`python:3.12-slim` plus uv) |
| Viewer | see 0012 |

### Layout

This replaces the top-level packages in the spec's repo layout.

```
pyproject.toml  uv.lock  .python-version  Makefile  docker-compose.yml  Dockerfile
src/fivefold/
  domain/      ids, operations, dependency rules; shared by gateway, sim and evaluator
  gateway/     MCP server, admin API, state machine, dispatcher, recovery
  sim/         procurement services, contracts, approvals, ledger
  harness/     scheduler, virtual clock, transports, run lifecycle, scripted humans
  faults/      catalog, plans, expected.yaml
  evaluator/   property checks, metrics, report
  agents/      LangGraph agent, model adapters, scripted policy, baselines
  recorder/    message capture, cassettes, playback
db/bootstrap.sql   roles; cluster level; idempotent
db/migrations/     NNNN_name.sql
tests/             unit/, pg/, property/, scenario/
scenarios/dev/
viewer/  tla/  docs/
```

Held-out scenarios never enter the repo. The scenario loader takes a path.

### Import boundaries

- `evaluator` imports `domain` only. It reads other components' tables, never their code.
- `gateway` never imports `sim`. It reaches the simulator only through the simulator's API.
- `agents` never import `gateway` or `sim`. They reach tools through MCP (G modes) or the baseline client (B modes).
- A test walks each module's imports with Python's `ast` module and fails on a violation.

### Determinism guards

- Ruff's `banned-api` rule (TID251, configured under `lint.flake8-tidy-imports.banned-api`; checked in ruff 0.17.5) bans these in `src/fivefold`: `time.time`, `time.monotonic`, `datetime.datetime.now`, `datetime.datetime.utcnow`, `datetime.date.today`, the `random` module's global functions, `uuid.uuid1`, `uuid.uuid4` and `os.urandom`. Only `harness/clock.py` and `harness/rng.py` are exempt. They hand out the virtual clock and seeded generators (0009).
- LangGraph's retry jitter draws from the global `random` module (checked in langgraph 1.2.22). Our code never seeds or reads that generator, so the jitter cannot shift our seeded streams.

### Migrations

- `db/bootstrap.sql` creates the roles in 0008. It runs once per cluster and is idempotent.
- Each `db/migrations/NNNN_name.sql` runs in one transaction. The runner records the file name and its SHA-256 in `public.schema_migration`, and refuses to continue if an applied file has changed.
- The runner migrates a template database. Each run's database is created from it (0009).
- Migrations only go forward. Run databases are disposable, so there are no down migrations.

### Make targets

`setup`, `fmt`, `lint`, `typecheck`, `test`, `test-fast` (no Postgres), `test-pg`, `check` (lint, typecheck and test), `db-up`, `db-reset`, `demo` (a stub until M1), `clean`.

### CI

- One workflow runs on every pull request and on pushes to `main`: lint, typecheck, then tests with a Postgres service container.
- Hypothesis uses a `ci` profile with `derandomize` on (the setting exists in hypothesis 6.170.15), so CI results are reproducible.
- A nightly job uses a randomized profile with more examples and uploads the seeds of any failures.
- Live-model runs never run in CI.

### Working agreement

- One ticket per pull request. An agent session works on one branch, opens one pull request at a time, and restarts the branch from `main` after each merge.
- Every pull request description names the spec section and decision records it implements, lists each library API used with where it was checked (a docs URL, or an installed source path and version), and states open doubts.
- Dependency upgrades are separate pull requests. An upgrade of LangGraph, langgraph-prebuilt or the MCP SDK must re-check the facts that 0008 and 0009 pin.

## Alternatives considered

| Area | Alternative | Why not chosen |
| --- | --- | --- |
| Locking | Poetry, pip-tools | Both work. uv is faster, also installs Python, and keeps one tool for environments and locks. |
| Types | pyright | Also strict and faster. mypy's Pydantic plugin understands model constructors, which this code uses heavily. |
| Database access | SQLAlchemy ORM | It hides transactions and row locks, which are what this project is about. |
| Migrations | Alembic | Standard and capable, but it brings SQLAlchemy and Python wrappers around SQL that is mostly roles, grants, triggers and partial indexes. |
| Migrations | dbmate, sqitch | Good plain-SQL tools, but each adds a non-Python binary to every environment to replace about 80 lines of runner code. |
| Postgres in tests | testcontainers | Works, but adds a dependency and a container start per session. The Compose service and CI service container already exist. |
| Layout | top-level `gateway/`, `sim/` packages (the spec) | Generic top-level names can collide with other installed packages. One `fivefold` package can be renamed in one place if the name changes. |

## Consequences

- The spec's repo layout becomes `src/fivefold/...`, with two new subpackages: `domain` and `harness`.
- Name check on 14 Nov 2026: `fivefold` is unclaimed on PyPI and npm. `agentcheck`, this repository's name, is taken on PyPI. On 14 Nov 2026 the maintainer chose the name Fivefold and the Apache-2.0 license.
- Spec changes on acceptance: the Stack table (uv, Ruff, mypy, psycopg, migrations) and the Repo layout.
