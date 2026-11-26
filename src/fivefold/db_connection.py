"""Build a component connection without carrying the administrator's credentials."""

from psycopg.conninfo import conninfo_to_dict, make_conninfo

_SERVER_KEYS = frozenset(
    {"host", "hostaddr", "port", "sslmode", "sslrootcert", "connect_timeout", "dbname"}
)


def role_dsn(
    source: str, role: str, *, dbname: str | None = None, default_timeout: int | None = None
) -> str:
    """Keep server coordinates and verification settings. Authenticate as the named role."""
    settings = conninfo_to_dict(source)
    if settings.get("service") and not (
        (settings.get("host") or settings.get("hostaddr")) and settings.get("port")
    ):
        raise ValueError("give the PostgreSQL host and port explicitly for component connections")
    params = {key: value for key, value in settings.items() if key in _SERVER_KEYS}
    if dbname is not None:
        params["dbname"] = dbname
    if default_timeout is not None:
        params.setdefault("connect_timeout", default_timeout)
    params["user"] = role
    return make_conninfo("", **params)
