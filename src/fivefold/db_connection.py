"""Build a component connection without carrying the administrator's credentials."""

from psycopg.conninfo import conninfo_to_dict, make_conninfo

_SERVER_KEYS = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "connect_timeout",
        "dbname",
        "sslmode",
        "sslrootcert",
        "sslcrl",
        "sslcrldir",
        "sslsni",
        "requirepeer",
        "ssl_min_protocol_version",
        "ssl_max_protocol_version",
        "channel_binding",
        "gssencmode",
    }
)


def server_parameters(source: str) -> dict[str, str]:
    """Keep explicit routing and verification settings for a component connection."""
    settings = conninfo_to_dict(source)
    if settings.get("service"):
        if not ((settings.get("host") or settings.get("hostaddr")) and settings.get("port")):
            raise ValueError(
                "give the PostgreSQL host and port explicitly for component connections"
            )
        if not (settings.get("sslmode") and settings.get("sslrootcert")):
            raise ValueError("give sslmode and sslrootcert explicitly with a PostgreSQL service")
    return {key: str(value) for key, value in settings.items() if key in _SERVER_KEYS}


def role_dsn(
    source: str, role: str, *, dbname: str | None = None, default_timeout: int | None = None
) -> str:
    """Keep server coordinates and verification settings. Authenticate as the named role."""
    params = server_parameters(source)
    if dbname is not None:
        params["dbname"] = dbname
    if default_timeout is not None:
        params.setdefault("connect_timeout", str(default_timeout))
    params["user"] = role
    return make_conninfo("", **params)
