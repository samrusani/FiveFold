"""Component connections inherit server settings without administrator credentials."""

import pytest
from psycopg.conninfo import conninfo_to_dict

from fivefold.viewer.source import RunSource


def test_viewer_replaces_credentials_and_keeps_connection_settings() -> None:
    source = RunSource(
        "host=local-db port=5433 dbname=initial user=administrator password=fixture"
        " sslmode=verify-full sslrootcert=/fixture/ca.pem sslcert=/fixture/client.crt"
        " sslkey=/fixture/client.key passfile=/fixture/passwords options='-c statement_timeout=10'"
    )
    assert conninfo_to_dict(source._conninfo("fivefold_run")) == {
        "host": "local-db",
        "port": "5433",
        "dbname": "fivefold_run",
        "user": "ff_viewer",
        "sslmode": "verify-full",
        "sslrootcert": "/fixture/ca.pem",
        "connect_timeout": "5",
    }


def test_a_service_without_explicit_coordinates_cannot_become_a_local_role_connection() -> None:
    source = RunSource("service=configured-server")
    with pytest.raises(ValueError, match="host and port explicitly"):
        source._conninfo("fivefold_run")
