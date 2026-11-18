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
    _comparison_fields,
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
    source = RunSource(
        "postgresql://postgres:" + "fixture-password" + "@db.example:6543/postgres?sslmode=require"
    )
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


def test_cell_key_uses_the_recorded_model_parameters_and_catalog_variants() -> None:
    def run(fault: str, variant: str | None, point: str, wait: int = 2**53) -> dict[str, object]:
        return {
            "scenario_digest": "same",
            "topology": "inproc",
            "model_adapter": "live",
            "model": "first",
            "parent_run_id": None,
            "branch_point": None,
            "params": {
                "params": {"approval_wait_ms": wait},
                "workers": 1,
                "plan": [{"fault": fault, "variant": variant, "at": {"point": point}}],
            },
        }

    early = run("F7", "price_up", "gw.dispatch.before_check")
    key = _comparison_fields(early)["cell_key"]
    assert key is not None
    assert _comparison_fields(run("F7", "price_up", "net.agent_sim.answer"))["cell_key"] == key
    assert _comparison_fields(run("F7", "price_up", "net.gw_sim.request"))["cell_key"] != key
    assert _comparison_fields(run("F7", "harmless", "gw.dispatch.before_check"))["cell_key"] != key
    assert _comparison_fields({**early, "model": "second"})["cell_key"] != key
    assert _comparison_fields({**early, "model_adapter": "replay"})["cell_key"] != key
    assert _comparison_fields({**early, "topology": "split"})["cell_key"] != key
    assert _comparison_fields({**early, "scenario_digest": "other"})["cell_key"] != key
    assert (
        _comparison_fields(run("F7", "price_up", "gw.dispatch.before_check", 2**53 + 1))["cell_key"]
        != key
    )
    for fault in ("F4", "F12"):
        direct = _comparison_fields(run(fault, None, "net.agent_sim.request"))
        forwarded = _comparison_fields(run(fault, None, "net.gw_sim.request"))
        gateway = _comparison_fields(run(fault, None, "net.agent_gw.request"))
        assert direct["planned_faults"] == [{"fault": fault, "variant": "agent_sim"}]
        assert forwarded["planned_faults"] == [{"fault": fault, "variant": "gw_sim"}]
        assert gateway["planned_faults"] == [{"fault": fault, "variant": "agent_gw"}]
        assert len({direct["cell_key"], forwarded["cell_key"], gateway["cell_key"]}) == 3


def test_wall_clock_times_become_iso_text_and_everything_else_stays() -> None:
    created = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)
    assert jsonable({"created": created, "voided": (1, 2), "body": {"a": [None, True]}}) == {
        "created": "2026-11-15T09:30:00+00:00",
        "voided": [1, 2],
        "body": {"a": [None, True]},
    }
