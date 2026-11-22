"""Run one scripted DBOS Workflow. All application I/O goes through FiveFold."""

from __future__ import annotations

import json
import sys
from typing import Any, TypedDict

from dbos import DBOS, DBOSConfig, SetWorkflowID
from dbos._error import DBOSMaxStepRetriesExceeded


class WorkflowInput(TypedDict):
    initial: dict[str, Any]
    max_steps: int
    max_tokens: int | None


class VirtualToolTimeout(Exception):
    """The FiveFold transport timed out on its virtual clock."""


def emit(message: dict[str, Any]) -> None:
    print(json.dumps(message), flush=True)


def handoff(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    step_id, status = DBOS.step_id, DBOS.step_status
    assert step_id is not None and status is not None and status.current_attempt is not None
    emit(
        {
            "kind": kind,
            "id": str(step_id),
            "attempt": status.current_attempt + 1,
            "payload": payload,
        }
    )
    raw = sys.stdin.readline()
    if not raw:
        raise EOFError("FiveFold closed the handoff")
    response = json.loads(raw)
    if response["retry"]:
        raise VirtualToolTimeout("FiveFold tool timeout")
    result: dict[str, Any] = response["result"]
    return result


def retry_timeout(error: BaseException) -> bool:
    return isinstance(error, VirtualToolTimeout)


@DBOS.step(retries_allowed=True, should_retry=retry_timeout)
def model_step(step: int, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return handoff("model", {"step": step, "messages": messages})


@DBOS.step(retries_allowed=True, should_retry=retry_timeout)
def tool_step(call: dict[str, Any]) -> dict[str, Any]:
    return handoff("tool", call)


@DBOS.step(retries_allowed=True, should_retry=retry_timeout)
def report_step(call: dict[str, Any]) -> dict[str, Any]:
    return handoff("report", call)


@DBOS.workflow()
def converse(spec: WorkflowInput) -> list[str]:
    messages = [spec["initial"]]
    tokens = 0
    for step in range(1, spec["max_steps"] + 1):
        if spec["max_tokens"] is not None and tokens >= spec["max_tokens"]:
            return ["budget", "Token budget used up."]
        result = model_step(step, messages)
        messages.append(result["message"])
        tokens += result["tokens"]
        data = result["message"]["data"]
        if not data["tool_calls"]:
            return ["finished", data["content"]]
        call = data["tool_calls"][0]
        invoke = report_step if call["name"] == "submit_final_report" else tool_step
        messages.append(invoke(call))
    return ["budget", "Step budget used up."]


def main() -> None:
    launch = json.loads(sys.stdin.readline())
    worker: str = launch["worker"]
    config: DBOSConfig = {
        "name": "fivefold-b2",
        "system_database_url": launch["system_database_url"],
        "dbos_system_schema": launch["schema"],
        "executor_id": worker,
        "application_version": "fivefold-b2-v1",
        "enable_otlp": False,
        "log_level": "ERROR",
        "sys_db_pool_size": 4,
    }
    DBOS(config=config)
    DBOS.launch()
    result: list[str]
    try:
        try:
            if launch["resume"]:
                result = DBOS.retrieve_workflow(worker).get_result()
            else:
                with SetWorkflowID(worker):
                    result = converse(launch["input"])
        except DBOSMaxStepRetriesExceeded:
            result = ["error", "DBOS Step exhausted its three attempts."]
        emit({"kind": "done", "result": result})
    finally:
        DBOS.destroy()


if __name__ == "__main__":
    main()
