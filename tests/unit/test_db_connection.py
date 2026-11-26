"""Component connections inherit server settings without administrator credentials."""

import pytest
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url

from fivefold.harness.dbos_worker import _state_url
from fivefold.harness.migrations import CreatedDatabase
from fivefold.viewer.source import RunSource

VERIFICATION = {
    "sslmode": "verify-full",
    "sslrootcert": "/fixture/ca.pem",
    "sslcrl": "/fixture/revoked.pem",
    "sslcrldir": "/fixture/revoked",
    "sslsni": "0",
    "requirepeer": "postgres",
    "ssl_min_protocol_version": "TLSv1.2",
    "ssl_max_protocol_version": "TLSv1.3",
    "channel_binding": "require",
    "gssencmode": "disable",
}


def test_viewer_replaces_credentials_and_keeps_connection_settings() -> None:
    source = RunSource(
        "host=local-db port=5433 dbname=initial user=administrator password=fixture"
        " sslcert=/fixture/client.crt"
        " sslkey=/fixture/client.key passfile=/fixture/passwords options='-c statement_timeout=10'"
        " " + " ".join(f"{key}={value}" for key, value in VERIFICATION.items())
    )
    assert conninfo_to_dict(source._conninfo("fivefold_run")) == {
        "host": "local-db",
        "port": "5433",
        "dbname": "fivefold_run",
        "user": "ff_viewer",
        **VERIFICATION,
        "connect_timeout": "5",
    }


def test_a_service_without_explicit_coordinates_cannot_become_a_local_role_connection() -> None:
    source = RunSource("service=configured-server")
    with pytest.raises(ValueError, match="host and port explicitly"):
        source._conninfo("fivefold_run")


@pytest.mark.parametrize("tls", ["", "sslmode=verify-full", "sslrootcert=/fixture/ca.pem"])
def test_service_aliases_need_explicit_tls_policy(tls: str) -> None:
    source = f"service=configured-server host=db port=5432 {tls}"
    with pytest.raises(ValueError, match="sslmode and sslrootcert explicitly"):
        RunSource(source)._conninfo("fivefold_run")
    with pytest.raises(ValueError, match="sslmode and sslrootcert explicitly"):
        _state_url(CreatedDatabase("fivefold_state", 1, source))


def test_dbos_uses_the_same_explicit_verification_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIVEFOLD_DBOS_PASSWORD", "worker-fixture")
    source = (
        "service=configured-server host=db port=5432 dbname=admin user=postgres password=fixture "
        + " ".join(f"{key}={value}" for key, value in VERIFICATION.items())
    )
    url = make_url(_state_url(CreatedDatabase("fivefold_state", 1, source)))
    assert url.username == "ff_dbos"
    assert url.database == "fivefold_state"
    assert url.password == "worker-fixture"
    assert dict(url.query) == {"host": "db", "port": "5432", **VERIFICATION}
