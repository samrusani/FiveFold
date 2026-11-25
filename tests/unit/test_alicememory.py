"""The AliceMemory exercise uses a fake HTTP transport in the deterministic suite."""

import json
import subprocess
import sys

import httpx
import pytest

from fivefold.harness.alicememory import FLAG, KEY, URL, USER_ID, main

BEARER = "synthetic_bearer_for_test"
USER = "05bfb352-e04b-4e8b-803b-b97c80f139df"
MEMORY = "e2e324a5-a0d7-41ea-9f27-9d705c7ffd61"
SETTINGS = {FLAG: "1", KEY: BEARER, URL: "http://127.0.0.1:8000", USER_ID: USER}
API = "/v0/vnext/memories"


def test_help_names_module_without_settings() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "fivefold.harness.alicememory", "--help"],
        env={},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert result.stdout.splitlines()[0].startswith("usage: python -m fivefold.harness.alicememory")
    assert result.stderr == ""


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SETTINGS.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (FLAG, "0", FLAG),
        (KEY, "", KEY),
        (URL, "", URL),
        (USER_ID, "", USER_ID),
        (URL, "https://example.com", "literal loopback address"),
        (URL, "http://127.0.0.1@example.com", "literal loopback address"),
        (URL, "http://127.0.0.1/path", "literal loopback address"),
        (URL, "http://localhost", "literal loopback address"),
    ],
)
def test_refuses_before_any_request(
    field: str,
    value: str,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(field, value)

    def no_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("the exercise contacted a server before validating its settings")

    assert main(transport=httpx.MockTransport(no_request)) == 1
    output = capsys.readouterr()
    assert error in output.err
    assert BEARER not in output.out + output.err


def _value(channel: str) -> str:
    return json.dumps({"notes": "FiveFold synthetic memory.", "order_channel": channel})


def _row(channel: str) -> dict[str, str]:
    return {"id": MEMORY, "user_id": USER, "canonical_text": _value(channel)}


def _revision(number: int, *, corrupt: bool = False) -> dict[str, int | str | None]:
    return {
        "memory_id": MEMORY,
        "revision_number": number,
        "revision_type": "created" if number == 1 else "corrected",
        "text_before": None if number == 1 else _value("other" if corrupt else "edi"),
        "text_after": _value("edi" if number == 1 else "portal"),
    }


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("corrupt", [False, True])
def test_uses_exact_routes_and_requires_retained_history(
    corrupt: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[tuple[str, str]] = []

    def request_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.headers["Authorization"] == f"Bearer {BEARER}"
        seen.append((request.method, path))
        if path == f"{API}/commit":
            body = json.loads(request.content)
            assert set(body) == {"user_id", "canonical_text", "title", "idempotency_key"}
            assert body["user_id"] == USER
            assert json.loads(body["canonical_text"]) == json.loads(_value("edi"))
            assert body["idempotency_key"] and body["title"]
            return httpx.Response(201, json={"status": "committed", "memory": _row("edi")})
        if path == f"{API}/correct":
            body = json.loads(request.content)
            assert set(body) == {"user_id", "canonical_text", "memory_id", "reason"}
            assert (body["memory_id"], body["user_id"]) == (MEMORY, USER)
            assert json.loads(body["canonical_text"]) == json.loads(_value("portal"))
            assert body["reason"]
            return httpx.Response(200, json={"status": "committed", "memory": _row("portal")})
        assert request.url.params["user_id"] == USER
        corrected = len(seen) == 4
        revisions = [_revision(1), _revision(2, corrupt=corrupt)] if corrected else [_revision(1)]
        return httpx.Response(
            200, json={"memory": _row("portal" if corrected else "edi"), "revisions": revisions}
        )

    assert main(transport=httpx.MockTransport(request_handler)) == int(corrupt)
    assert seen == [
        ("POST", f"{API}/commit"),
        ("GET", f"{API}/{MEMORY}/audit"),
        ("POST", f"{API}/correct"),
        ("GET", f"{API}/{MEMORY}/audit"),
    ]
    output = capsys.readouterr()
    assert BEARER not in output.out + output.err
    if corrupt:
        assert "did not retain the previous value" in output.err
    else:
        assert "Version 1 edi to version 2 portal" in output.out


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"reason": BEARER}),
        (302, {"reason": BEARER}),
        (200, {"status": "rejected", "reason": BEARER}),
        (201, {"status": "committed", "memory": {"id": MEMORY, "token": BEARER}}),
        (201, {"status": "committed", "memory": {**_row("edi"), "user_id": MEMORY}}),
    ],
)
def test_refused_or_incomplete_commit_fails_without_leaking_credentials(
    status: int,
    body: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://example.com"}, json=body)

    assert main(transport=httpx.MockTransport(refuse)) == 1
    assert len(attempts) == 1
    output = capsys.readouterr()
    assert "AliceMemory exercise failed" in output.err
    assert BEARER not in output.out + output.err
