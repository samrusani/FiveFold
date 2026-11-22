"""Run databases are copies of a migrated template database (0007, 0009).

`fivefold.harness.migrations.create_run_database` makes the template if it is missing,
migrates it forward if it is behind, and copies it, in one hold of the database lock. The
demo, the recorder, the study and the split topology's `start` all make their databases
this way. Each test here names its own template, so none changes the template the other
tests copy (`PREFIX_simtpl`), nor the one a person's runs are made from.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from fivefold.domain.contracts import Contract
from fivefold.harness import demo
from fivefold.harness.demo import DEMO_BY_KEY, DEV_001
from fivefold.harness.migrations import (
    RUN_DATABASE_COMMENT,
    TEMPLATE_ENV,
    CreatedDatabase,
    MigrationError,
    create_run_database,
    database_lock,
    discover,
    drop_database,
    migrate,
    prepare_template,
    recreate_database,
)
from fivefold.harness.registry import forget
from fivefold.harness.split.lifecycle import start_run
from fivefold.harness.study import main as study_main
from fivefold.recorder.__main__ import main as recorder_main

pytestmark = pytest.mark.pg

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"
MARK = "copied_from_the_template"
"""A table only the marked template holds, so a database that holds it is its copy."""


def applied(dsn: str) -> list[tuple[Any, ...]]:
    """A database's migration rows, with when each was applied: a copy keeps the
    template's times."""
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT filename, sha256, applied_at FROM public.schema_migration ORDER BY filename"
        ).fetchall()


def copied(admin_dsn: str, name: str) -> bool:
    """Whether the database `name` holds the marked template's table."""
    with psycopg.connect(make_conninfo(admin_dsn, dbname=name)) as conn:
        row = conn.execute("SELECT to_regclass(%s)", (f"public.{MARK}",)).fetchone()
    return row is not None and row[0] is not None


def exists(admin_dsn: str, name: str) -> bool:
    with psycopg.connect(admin_dsn) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


@pytest.fixture
def drop_after(admin_dsn: str) -> Iterator[list[str]]:
    """Names of databases, and their registry rows, to drop when the test ends."""
    names: list[str] = []
    yield names
    for name in names:
        drop_database(admin_dsn, name)
        forget(admin_dsn, name)


@pytest.fixture
def marked_template(
    admin_dsn: str, db_prefix: str, monkeypatch: pytest.MonkeyPatch, drop_after: list[str]
) -> str:
    """A migrated template holding one more table, `MARK`, named by FIVEFOLD_TEMPLATE_DB
    while the test runs, so every command copies it by default."""
    name = f"{db_prefix}_marktpl"
    drop_database(admin_dsn, name)
    drop_after.append(name)
    prepare_template(admin_dsn, MIGRATIONS, name)
    with psycopg.connect(make_conninfo(admin_dsn, dbname=name), autocommit=True) as conn:
        conn.execute(f"CREATE TABLE public.{MARK} ()")
    monkeypatch.setenv(TEMPLATE_ENV, name)
    return name


# Each command copies the template ---------------------------------------------------------


def test_the_demo_runs_in_a_copy_of_the_template(
    admin_dsn: str,
    db_prefix: str,
    marked_template: str,
    monkeypatch: pytest.MonkeyPatch,
    drop_after: list[str],
) -> None:
    def prefixed(admin: str, name: str, migrations: Path, **options: Any) -> CreatedDatabase:
        """The demo's database, under this test run's prefix like every test database."""
        drop_after.append(f"{db_prefix}_{name}")
        return create_run_database(admin, drop_after[-1], migrations, **options)

    monkeypatch.setattr(demo, "create_run_database", prefixed)
    summary = demo.run_demo(admin_dsn, DEMO_BY_KEY["1"], "G2", 7)
    assert summary.verdict.startswith("pass")
    assert copied(admin_dsn, drop_after[-1])


def test_each_recorder_command_runs_in_a_copy_of_the_template(
    admin_dsn: str, db_prefix: str, marked_template: str, tmp_path: Path, drop_after: list[str]
) -> None:
    recorded, replayed, branched = (f"{db_prefix}_tpl_{c}" for c in ("rec", "play", "branch"))
    drop_after.extend([recorded, replayed, branched])
    cassette = tmp_path / "demo-1.json"
    common = ["--dsn", admin_dsn]
    record = [*common, "record", "--demo", "1", "--config", "G2", "--cassette", str(cassette)]
    assert recorder_main([*record, "--database", recorded]) == 0
    play = [*common, "play", str(cassette), "--scenario", str(DEV_001)]
    assert recorder_main([*play, "--original", recorded, "--database", replayed]) == 0
    branch = [*common, "branch", str(cassette), "--scenario", str(DEV_001), "--at", "4"]
    branch += ["--cassette", str(tmp_path / "branch.json"), "--result", str(tmp_path / "r.json")]
    assert recorder_main([*branch, "--database", branched]) == 0
    assert [copied(admin_dsn, name) for name in (recorded, replayed, branched)] == [True] * 3


@pytest.mark.parametrize("replace_existing", [False, True])
def test_run_commands_keep_a_database_they_did_not_create(
    admin_dsn: str,
    db_prefix: str,
    marked_template: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drop_after: list[str],
    capsys: pytest.CaptureFixture[str],
    replace_existing: bool,
) -> None:
    original, foreign = f"{db_prefix}_guard_orig", f"{db_prefix}_guard_foreign"
    drop_after.extend((original, foreign))
    foreign_dsn = recreate_database(admin_dsn, foreign)
    with psycopg.connect(foreign_dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE public.sentinel (value integer)")
        conn.execute("INSERT INTO public.sentinel VALUES (17)")

    cassette = tmp_path / "original.json"
    common = ["--dsn", admin_dsn]
    record = ["record", "--demo", "1", "--config", "G2", "--cassette", str(cassette)]
    assert recorder_main([*common, *record, "--database", original]) == 0
    capsys.readouterr()
    options = ["--replace"] if replace_existing else []
    for argv in (
        record,
        ["play", str(cassette), "--scenario", str(DEV_001), "--original", original],
        [
            "branch",
            str(cassette),
            "--scenario",
            str(DEV_001),
            "--at",
            "1",
            "--cassette",
            str(tmp_path / "branch.json"),
            "--result",
            str(tmp_path / "result.json"),
        ],
    ):
        assert recorder_main([*common, *argv, "--database", foreign, *options]) == 1
        error = capsys.readouterr().err
        assert foreign in error and "unused" in error

    first_demo = f"{db_prefix}_guard_demo"

    monkeypatch.setattr(
        demo, "_demo_database", lambda _demo, config: foreign if config == "G2" else first_demo
    )
    drop_after.append(first_demo)
    assert demo.main([*common, "1"]) == 1
    assert foreign in capsys.readouterr().err
    assert not exists(admin_dsn, first_demo)
    with psycopg.connect(foreign_dsn) as conn:
        assert conn.execute("SELECT value FROM public.sentinel").fetchall() == [(17,)]


@pytest.mark.parametrize("legacy", ["0.1.0", "current"])
def test_demo_replaces_legacy_and_marked_databases_on_rerun(
    legacy: str,
    admin_dsn: str,
    db_prefix: str,
    marked_template: str,
    monkeypatch: pytest.MonkeyPatch,
    drop_after: list[str],
    tmp_path: Path,
) -> None:
    names = {config: f"{db_prefix}_demo_{config.lower()}" for config in ("B0", "G2")}
    drop_after.extend(names.values())
    monkeypatch.setattr(demo, "_demo_database", lambda _demo, config: names[config])
    migrations = MIGRATIONS
    if legacy == "0.1.0":
        for migration in discover(MIGRATIONS):
            if migration.number <= 14:
                shutil.copyfile(migration.path, tmp_path / migration.name)
        migrations = tmp_path
    with psycopg.connect(recreate_database(admin_dsn, names["B0"]), autocommit=True) as conn:
        migrate(conn, migrations)

    def databases() -> dict[str, tuple[int, str | None]]:
        with psycopg.connect(admin_dsn) as conn:
            rows = conn.execute(
                "SELECT datname, oid, pg_catalog.shobj_description(oid, 'pg_database')"
                " FROM pg_catalog.pg_database WHERE datname = ANY(%s)",
                (list(names.values()),),
            ).fetchall()
        return {name: (oid, comment) for name, oid, comment in rows}

    old_oid, old_comment = databases()[names["B0"]]
    assert old_comment is None
    args = ["--dsn", admin_dsn, "1"]
    assert demo.main(args) == 0
    first = databases()
    assert first[names["B0"]][0] != old_oid
    assert demo.main(args) == 0
    second = databases()
    for name in names.values():
        assert first[name][1] == second[name][1] == RUN_DATABASE_COMMENT
        assert first[name][0] != second[name][0]


@pytest.mark.parametrize(
    "change", ["checksum", "missing_file", "extra_file", "view", "arbitrary_prefix"]
)
def test_replacement_requires_complete_legacy_history_in_a_table(
    admin_dsn: str,
    db_prefix: str,
    marked_template: str,
    drop_after: list[str],
    change: str,
) -> None:
    name = f"{db_prefix}_legacy_{change}"
    drop_after.append(name)
    with psycopg.connect(recreate_database(admin_dsn, name), autocommit=True) as conn:
        migrate(conn, MIGRATIONS)
        first = discover(MIGRATIONS)[0].name
        if change == "checksum":
            conn.execute(
                "UPDATE public.schema_migration SET sha256 = 'old' WHERE filename = %s", (first,)
            )
        elif change == "missing_file":
            conn.execute("DELETE FROM public.schema_migration WHERE filename = %s", (first,))
        elif change == "arbitrary_prefix":
            conn.execute("DELETE FROM public.schema_migration WHERE filename >= '0014_'")
        elif change == "extra_file":
            conn.execute(
                "INSERT INTO public.schema_migration (filename, sha256)"
                " VALUES ('unrelated.sql', 'extra')"
            )
        else:
            conn.execute("ALTER TABLE public.schema_migration RENAME TO actual_migrations")
            conn.execute(
                "CREATE VIEW public.schema_migration AS"
                " SELECT filename, sha256 FROM public.actual_migrations"
            )
    with pytest.raises(MigrationError, match=f"{name} is not a run database"):
        create_run_database(admin_dsn, name, MIGRATIONS, replace=True)
    assert exists(admin_dsn, name)


def test_run_database_replacement_requires_an_explicit_request(
    admin_dsn: str, db_prefix: str, marked_template: str, drop_after: list[str]
) -> None:
    name = f"{db_prefix}_owned_run"
    drop_after.append(name)
    created = create_run_database(admin_dsn, name, MIGRATIONS)
    with pytest.raises(MigrationError, match=f"{name} already exists.*--replace"):
        create_run_database(admin_dsn, name, MIGRATIONS)
    recreated = create_run_database(admin_dsn, name, MIGRATIONS, replace=True)
    assert recreated.oid != created.oid
    assert copied(admin_dsn, name)


def test_play_checks_the_original_before_touching_the_destination(
    admin_dsn: str,
    db_prefix: str,
    marked_template: str,
    tmp_path: Path,
    drop_after: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    original, wrong, dest, unused = (f"{db_prefix}_preflight_{key}" for key in "owdu")
    drop_after.extend((original, wrong, dest, unused))
    cassette = tmp_path / "original.json"
    common = ["--dsn", admin_dsn]
    assert (
        recorder_main(
            [
                *common,
                "record",
                "--demo",
                "1",
                "--config",
                "G2",
                "--cassette",
                str(cassette),
                "--database",
                original,
            ]
        )
        == 0
    )
    create_run_database(admin_dsn, wrong, MIGRATIONS)
    existing = create_run_database(admin_dsn, dest, MIGRATIONS)
    play = [*common, "play", str(cassette), "--scenario", str(DEV_001), "--original", wrong]
    for name in (dest, unused):
        assert recorder_main([*play, "--database", name, "--replace"]) == 1
        assert "the original run's database holds no run" in capsys.readouterr().err
    assert not exists(admin_dsn, unused)
    with psycopg.connect(admin_dsn) as conn:
        row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (dest,)).fetchone()
    assert row == (existing.oid,)


def test_a_study_episode_runs_in_a_copy_of_the_template(
    admin_dsn: str, db_prefix: str, marked_template: str, tmp_path: Path, drop_after: list[str]
) -> None:
    prefix = f"{db_prefix}_tpl_study"
    drop_after.append(f"{prefix}_0001")
    argv = ["--dsn", admin_dsn, "--out", str(tmp_path / "out"), "--database-prefix", prefix]
    argv += ["--cells", "none", "--configs", "G2", "--contracts", "C2", "--keep-databases"]
    assert study_main(argv) == 0
    assert copied(admin_dsn, f"{prefix}_0001")


def test_a_split_start_copies_the_template_the_environment_names(
    admin_dsn: str, db_prefix: str, marked_template: str, tmp_path: Path, drop_after: list[str]
) -> None:
    name = f"{db_prefix}_tpl_split"
    drop_after.append(name)
    start_run(
        admin_dsn=admin_dsn,
        scenario_path=DEV_001,
        config="B0",
        contract=Contract.C2,
        seed=7,
        run_dir=tmp_path / "run",
        plan=[],
        database=name,
    )
    assert copied(admin_dsn, name)


# The template ---------------------------------------------------------------------------


def test_a_template_behind_the_files_is_migrated_forward_before_it_is_copied(
    admin_dsn: str, db_prefix: str, tmp_path: Path, drop_after: list[str]
) -> None:
    template, run = f"{db_prefix}_behindtpl", f"{db_prefix}_behindrun"
    drop_after.extend([run, template])
    every = discover(MIGRATIONS)
    behind = tmp_path / "behind"
    behind.mkdir()
    for migration in every[:-1]:
        shutil.copy(migration.path, behind / migration.name)
    with psycopg.connect(recreate_database(admin_dsn, template), autocommit=True) as conn:
        migrate(conn, behind)
    created = create_run_database(admin_dsn, run, MIGRATIONS, template=template)
    template_dsn = make_conninfo(admin_dsn, dbname=template)
    rows = applied(template_dsn)
    assert [(name, sha) for name, sha, _ in rows] == [(m.name, m.sha256) for m in every]
    assert applied(created.dsn) == rows, "a copy of the migrated template, times included"
    with psycopg.connect(created.dsn, autocommit=True) as conn:
        assert migrate(conn, MIGRATIONS) == [], "a copy keeps its migration rows"


def test_a_template_whose_files_changed_is_refused_in_one_line(
    admin_dsn: str, db_prefix: str, tmp_path: Path, drop_after: list[str]
) -> None:
    template, run = f"{db_prefix}_changedtpl", f"{db_prefix}_changedrun"
    drop_after.extend([run, template])
    files = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, files)
    prepare_template(admin_dsn, files, template)
    last = sorted(files.glob("*.sql"))[-1]
    last.write_text(last.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    with pytest.raises(MigrationError) as refused:
        create_run_database(admin_dsn, run, files, template=template)
    message = str(refused.value)
    assert "\n" not in message
    assert f"the template database {template} cannot be used" in message
    assert f"{last.name} changed after it was applied" in message
    assert not exists(admin_dsn, run)


def test_two_copies_at_once_both_work_and_the_template_is_made_once(
    admin_dsn: str,
    db_prefix: str,
    await_database_lock: Callable[[str], None],
    drop_after: list[str],
) -> None:
    """Two callers, each with its own connections as two processes have, copy a template
    that does not exist yet. Both wait for the database lock; the first makes and
    migrates the template, closes its connection to it and copies it, then the second
    finds it ready and copies it too."""
    template = f"{db_prefix}_racetpl"
    runs = [f"{db_prefix}_race_{n}" for n in (1, 2)]
    drop_after.extend([*runs, template])
    drop_database(admin_dsn, template)
    made: list[CreatedDatabase] = []
    failed: list[BaseException] = []

    def copy(name: str) -> None:
        try:
            dsn = make_conninfo(admin_dsn, application_name=name)
            made.append(create_run_database(dsn, name, MIGRATIONS, template=template))
        except BaseException as error:  # reported by the assertion below
            failed.append(error)

    threads = [threading.Thread(target=copy, args=(name,)) for name in runs]
    with database_lock(admin_dsn):
        for thread, name in zip(threads, runs, strict=True):
            thread.start()
            await_database_lock(name)
    for thread in threads:
        thread.join(timeout=60)
    assert failed == []
    assert sorted(created.name for created in made) == runs
    rows = applied(make_conninfo(admin_dsn, dbname=template))
    assert len(rows) == len(discover(MIGRATIONS))
    assert [applied(created.dsn) for created in made] == [rows, rows]


def test_a_template_another_session_holds_is_refused_in_one_line(
    admin_dsn: str,
    db_prefix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    drop_after: list[str],
) -> None:
    """PostgreSQL copies a database only while no other session is connected to it: it
    waits up to 5 seconds for one to end, then refuses. The recorder says so in one line."""
    template, run = f"{db_prefix}_busytpl", f"{db_prefix}_busyrun"
    drop_after.extend([run, template])
    monkeypatch.setenv(TEMPLATE_ENV, prepare_template(admin_dsn, MIGRATIONS, template))
    argv = ["--dsn", admin_dsn, "record", "--demo", "1", "--config", "B0", "--database", run]
    argv += ["--cassette", str(tmp_path / "cassette.json")]
    with psycopg.connect(make_conninfo(admin_dsn, dbname=template)):
        assert recorder_main(argv) == 1
    error = capsys.readouterr().err
    assert error == (
        f"record failed: the template database {template} is in use by another session,"
        " so it cannot be copied; close that session and try again\n"
    )
    assert not exists(admin_dsn, run)


def test_a_run_database_named_as_the_template_is_refused(
    admin_dsn: str,
    db_prefix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    drop_after: list[str],
) -> None:
    """Replacing the template with a run would make every later copy start with that run."""
    template = f"{db_prefix}_ownedtpl"
    drop_after.append(template)
    monkeypatch.setenv(TEMPLATE_ENV, prepare_template(admin_dsn, MIGRATIONS, template))
    before = applied(make_conninfo(admin_dsn, dbname=template))
    argv = ["--dsn", admin_dsn, "record", "--demo", "1", "--config", "B0", "--database"]
    argv += [template, "--cassette", str(tmp_path / "cassette.json")]
    assert recorder_main(argv) == 1
    assert capsys.readouterr().err == (
        f"record failed: the database {template} is the template run databases are copied from\n"
    )
    assert applied(make_conninfo(admin_dsn, dbname=template)) == before
    with psycopg.connect(make_conninfo(admin_dsn, dbname=template)) as conn:
        assert conn.execute("SELECT count(*) FROM harness.run").fetchone() == (0,)
