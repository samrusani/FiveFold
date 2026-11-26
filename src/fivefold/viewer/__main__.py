"""Serve the viewer: `python -m fivefold.viewer` (0012).

    python -m fivefold.viewer                  # http://127.0.0.1:8300
    python -m fivefold.viewer --port 8400 --registry fivefold_registry

It reads the run registry and the run databases on the server FIVEFOLD_ADMIN_DSN names
(default: the local Docker Compose database), always as `ff_viewer`, whatever user that
string names. FIVEFOLD_REGISTRY_DB, or `--registry`, names another registry. It serves the
frontend from `viewer/dist`, which `npm run build` in `viewer/` makes; without it, the API
still answers.

Checked in uvicorn 0.54.0: `uvicorn.run(app, host=..., port=...)` (`main.py`).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import uvicorn

from fivefold.viewer.app import DEFAULT_STATIC, create_app
from fivefold.viewer.source import REGISTRY_ENV, RunSource, registry_name

DEFAULT_SERVER: Final = "postgresql://127.0.0.1:5432"
"""The local Docker Compose database, when FIVEFOLD_ADMIN_DSN is unset."""

DEFAULT_PORT: Final = 8300


def main(argv: Sequence[str] | None = None) -> int:
    """Serve the viewer until interrupted. Returns the process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m fivefold.viewer",
        description="Serve the read-only viewer of runs: the run list, timelines,"
        " scorecards, and B0 beside G2.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to listen on")
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME",
        help="also accept this exact HTTP host name or address without a port (repeatable)",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("FIVEFOLD_ADMIN_DSN", DEFAULT_SERVER),
        help="connection string of the server with the runs; the viewer connects as"
        " ff_viewer whatever user it names (default: FIVEFOLD_ADMIN_DSN)",
    )
    parser.add_argument(
        "--registry",
        default=None,
        help="the run registry's database (default: FIVEFOLD_REGISTRY_DB, or fivefold_registry)",
    )
    parser.add_argument(
        "--static",
        type=Path,
        default=DEFAULT_STATIC,
        help="the frontend's built files (default: viewer/dist)",
    )
    args = parser.parse_args(argv)
    try:
        registry = registry_name(
            os.environ if args.registry is None else {REGISTRY_ENV: args.registry}
        )
        app = create_app(RunSource(args.dsn, registry), args.static, extra_hosts=args.allow_host)
    except ValueError as error:
        parser.error(str(error))
    address = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    print(f"Viewer on http://{address}:{args.port}/ (registry {registry})", flush=True)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
