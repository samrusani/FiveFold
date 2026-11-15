"""The viewer's read-only HTTP API, and the built frontend it serves (0012).

Every endpoint is a GET and answers JSON:

- `/api/runs`: the run list, from the run registry, newest first.
- `/api/runs/{database}`: one run: its registry row, `harness.run`, when it stopped, and
  its scorecard from `eval` (the verdict, each property's verdict, reason codes and
  failures, the excused intents and the judged claims), or null before it is evaluated.
- `/api/runs/{database}/timeline`: every message with its fate, the faults that fired,
  the scripted humans' actions, the reports and the gateway's action events, in record
  0012's order, each event marked when it shares its virtual time with another
  (`fivefold.viewer.timeline`).
- `/api/runs/{database}/ledger`: the effects ledger and the fact history.

A run is named by its database, because two databases can hold runs with the same id,
such as a recording and its replay. The viewer opens only databases the registry lists.

An error answers JSON too, `{"error": CODE, "detail": TEXT}`: 404 `unknown_run` (the
registry lists no such database), 404 `run_unavailable` (it cannot be read, for example
because it was dropped), 404 `not_a_run` (it holds no run), and 503
`registry_unavailable`.

Every other path serves the frontend's built files (`viewer/dist`, from `npm run build`)
when they exist. The frontend routes in the URL's fragment, so `/` is its only page.

Checked in starlette 1.10.0: `Starlette(routes=..., exception_handlers=...)`
(`applications.py`); `Route(path, endpoint, methods=...)`, which runs a plain function
in a thread pool, and `Mount(path, app=..., name=...)` (`routing.py`);
`StaticFiles(directory=..., html=True)` (`staticfiles.py`); `JSONResponse` and
`HTMLResponse` (`responses.py`); `Request.path_params` (`requests.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from fivefold.viewer import records
from fivefold.viewer.source import RunSource, ViewerError
from fivefold.viewer.timeline import merge

DEFAULT_STATIC: Final = Path(__file__).resolve().parents[3] / "viewer" / "dist"
"""Where `npm run build` in `viewer/` puts the frontend."""

NO_STORE: Final = {"Cache-Control": "no-store"}
"""A run that is still going on changes between two requests."""

NOT_BUILT: Final = (
    "<!doctype html><title>Fivefold viewer</title>"
    "<p>The viewer's frontend is not built. Run <code>npm ci</code> and"
    " <code>npm run build</code> in <code>viewer/</code>, then start the viewer again."
    " The API answers under <code>/api/</code>.</p>"
)


def create_app(source: RunSource, static: Path | None = DEFAULT_STATIC) -> Starlette:
    """The viewer: its API over `source`, and the frontend from `static` if it exists."""

    def run_list(request: Request) -> Response:
        return _json({"registry": source.registry, "runs": source.runs()})

    def run_summary(request: Request) -> Response:
        database = str(request.path_params["database"])
        registered = source.registered(database)
        with source.run(database) as conn:
            run = records.run(conn)
            card = records.scorecard(conn, str(run["run_id"]))
        return _json({"database": database, "registry": registered, "run": run, "scorecard": card})

    def run_timeline(request: Request) -> Response:
        database = str(request.path_params["database"])
        with source.run(database) as conn:
            records.run(conn)
            events = merge(records.timeline_events(conn))
        return _json({"database": database, "events": events})

    def run_ledger(request: Request) -> Response:
        database = str(request.path_params["database"])
        with source.run(database) as conn:
            records.run(conn)
            ledger = records.ledger(conn)
        return _json({"database": database, **ledger})

    def not_built(request: Request) -> Response:
        return HTMLResponse(NOT_BUILT, status_code=404)

    routes: list[BaseRoute] = [
        Route("/api/runs", run_list, methods=["GET"]),
        Route("/api/runs/{database}", run_summary, methods=["GET"]),
        Route("/api/runs/{database}/timeline", run_timeline, methods=["GET"]),
        Route("/api/runs/{database}/ledger", run_ledger, methods=["GET"]),
    ]
    if static is not None and static.is_dir():
        routes.append(Mount("/", app=StaticFiles(directory=static, html=True), name="frontend"))
    else:
        routes.append(Route("/", not_built, methods=["GET"]))
    return Starlette(routes=routes, exception_handlers={ViewerError: _viewer_error})


def _json(content: Any) -> Response:
    return JSONResponse(content, headers=NO_STORE)


def _viewer_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ViewerError):
        raise exc
    return JSONResponse(
        {"error": exc.code, "detail": exc.detail}, status_code=exc.status, headers=NO_STORE
    )
