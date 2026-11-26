"""Naming the run registry, the template database and a run's database (0009, 0012), with
no database."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest

from fivefold.harness.migrations import (
    REGISTRY_DATABASE,
    REGISTRY_ENV,
    TEMPLATE_DATABASE,
    TEMPLATE_ENV,
    MigrationError,
    registry_database,
    template_database,
)
from fivefold.harness.registry import RegistryError, database_of


def test_the_registry_is_fivefold_registry_unless_the_environment_names_another() -> None:
    assert REGISTRY_DATABASE == "fivefold_registry"
    assert registry_database({}) == "fivefold_registry"
    assert registry_database({REGISTRY_ENV: ""}) == "fivefold_registry"
    assert registry_database({REGISTRY_ENV: "fivefold_test_registry"}) == "fivefold_test_registry"


@pytest.mark.parametrize("name", ["Fivefold", "a" * 64, "registry-1"])
def test_a_registry_name_postgres_would_change_is_refused(name: str) -> None:
    with pytest.raises(MigrationError):
        registry_database({REGISTRY_ENV: name})


def test_the_template_is_fivefold_template_unless_the_environment_names_another() -> None:
    assert TEMPLATE_DATABASE == "fivefold_template"
    assert template_database({}) == "fivefold_template"
    assert template_database({TEMPLATE_ENV: ""}) == "fivefold_template"
    assert template_database({TEMPLATE_ENV: "fivefold_test_simtpl"}) == "fivefold_test_simtpl"
    with pytest.raises(MigrationError):
        template_database({TEMPLATE_ENV: "template-1"})


def test_a_run_database_is_named_by_its_connection_string() -> None:
    assert database_of("postgresql://postgres@127.0.0.1:5432/fivefold_demo_1_g2") == (
        "fivefold_demo_1_g2"
    )
    assert database_of("host=127.0.0.1 dbname=run_0001 user=postgres") == "run_0001"


def test_a_connection_string_without_a_database_is_refused() -> None:
    with pytest.raises(RegistryError, match="names no database"):
        database_of("postgresql://postgres@127.0.0.1:5432")


@pytest.mark.parametrize(
    "names",
    [
        {TEMPLATE_ENV: REGISTRY_DATABASE},
        {TEMPLATE_ENV: "fivefold_dev"},
        {REGISTRY_ENV: TEMPLATE_DATABASE},
        {REGISTRY_ENV: "fivefold_dev"},
        {TEMPLATE_ENV: "shared_database", REGISTRY_ENV: "shared_database"},
    ],
)
@pytest.mark.parametrize("read_name", [template_database, registry_database])
def test_reserved_database_names_cannot_collide(
    read_name: Callable[[Mapping[str, str]], str], names: dict[str, str]
) -> None:
    with pytest.raises(MigrationError, match=r"reserved|distinct") as error:
        read_name(names)
    assert "drop" not in str(error.value).lower()
