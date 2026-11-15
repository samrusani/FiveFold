"""The viewer's connections and names (0012), with no database: it always connects as
ff_viewer, and it names the same registry as the harness."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from psycopg.conninfo import conninfo_to_dict

from fivefold.harness import migrations
from fivefold.viewer.source import (
    CONNECT_TIMEOUT_S,
    DEFAULT_REGISTRY,
    REGISTRY_ENV,
    VIEWER_ROLE,
    RunSource,
    jsonable,
    registry_name,
)


def test_the_viewer_and_the_harness_name_the_same_registry() -> None:
    assert DEFAULT_REGISTRY == migrations.REGISTRY_DATABASE
    assert REGISTRY_ENV == migrations.REGISTRY_ENV
    assert registry_name({}) == DEFAULT_REGISTRY
    assert registry_name({REGISTRY_ENV: "fivefold_test_registry"}) == "fivefold_test_registry"


def test_a_registry_name_the_harness_cannot_make_is_refused() -> None:
    with pytest.raises(ValueError, match="not a database name"):
        registry_name({REGISTRY_ENV: "Registry; DROP"})


def test_the_viewer_connects_as_ff_viewer_whatever_user_the_server_string_names() -> None:
    source = RunSource("postgresql://postgres:secret@db.example:6543/postgres?sslmode=require")
    params = conninfo_to_dict(source._conninfo("fivefold_demo_1_g2"))
    assert params["user"] == VIEWER_ROLE == "ff_viewer"
    assert params["dbname"] == "fivefold_demo_1_g2"
    assert "password" not in params, "the server string's password is not ff_viewer's"
    assert (params["host"], params["port"], params["sslmode"]) == ("db.example", "6543", "require")


def test_a_connect_timeout_in_the_server_string_is_kept_and_passed_once() -> None:
    source = RunSource("postgresql://postgres@db.example/postgres?connect_timeout=3")
    params = conninfo_to_dict(source._conninfo("fivefold_demo_1_g2"))
    assert (params["connect_timeout"], params["user"]) == ("3", VIEWER_ROLE)


def test_without_one_the_viewer_sets_its_own_connect_timeout() -> None:
    params = conninfo_to_dict(RunSource("postgresql://postgres@db.example/postgres")._conninfo("x"))
    assert params["connect_timeout"] == str(CONNECT_TIMEOUT_S)


def test_wall_clock_times_become_iso_text_and_everything_else_stays() -> None:
    created = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)
    assert jsonable({"created": created, "voided": (1, 2), "body": {"a": [None, True]}}) == {
        "created": "2026-11-15T09:30:00+00:00",
        "voided": [1, 2],
        "body": {"a": [None, True]},
    }
