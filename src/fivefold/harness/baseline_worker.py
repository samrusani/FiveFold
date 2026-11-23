"""Pass scripted baseline worker calls through FiveFold's virtual scheduler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, messages_from_dict
from pydantic import ValidationError

from fivefold.agents.tools import GATEWAY_WRITES, baseline_tools
from fivefold.agents.worker import WorkerEnd, tokens_used
from fivefold.domain.contracts import Capability
from fivefold.harness.transport import TransportTimeout
from fivefold.recorder.cassette import canonical_json, sha256

if TYPE_CHECKING:
    from fivefold.harness.run import InprocRun


@dataclass(frozen=True)
class Request:
    kind: str
    activity_id: str
    attempt: int
    task_token: bytes
    payload: dict[str, Any]


@dataclass(frozen=True)
class Done:
    result: list[str]


@dataclass(frozen=True)
class Broken:
    error: BaseException


Delivery = Request | Done | Broken
Reply = tuple[Request, Any, str | None]


class BaselineWorkers:
    """Run the same direct model and tools for each durable execution backend."""

    def __init__(
        self, run: InprocRun, infrastructure_error: type[RuntimeError], workflow_failure: str
    ) -> None:
        self.run = run
        self.infrastructure_error = infrastructure_error
        self.workflow_failure = workflow_failure
        self.active: dict[str, Request] = {}
        self.handoff = asyncio.Lock()

    async def work(self, worker: str, *, resume: bool) -> None:
        run = self.run
        tools = baseline_tools(
            run.transport,
            run.scheduler,
            run.timeouts,
            worker=worker,
            status=run.spec.contract.has(Capability.STATUS),
        )
        by_name = {tool.name: tool for tool in tools}
        bound = run._chat_model(worker).bind_tools(tools)
        reply: Reply | None = None
        if resume:
            reply = (self.active[worker], None, "Virtual agent worker lost before completion")

        while True:
            command = await run.scheduler.hold(
                self._wait_for_handoff(worker, reply, start=not resume and reply is None)
            )
            if isinstance(command, Done):
                run.result.ends[worker] = (WorkerEnd(command.result[0]), command.result[1])
                return
            if isinstance(command, Broken):
                raise self.infrastructure_error(self.workflow_failure) from command.error

            self.active[worker] = command
            try:
                if command.kind == "model":
                    asked = run._call_log(worker)()
                    messages = messages_from_dict(command.payload["messages"])
                    result = await bound.ainvoke(messages)
                    if not isinstance(result, AIMessage) or len(result.tool_calls) > 1:
                        raise self.infrastructure_error(
                            "the scripted model returned an unsupported reply"
                        )
                    asked(result)
                    run._agent_step(worker, command.payload["step"])
                    reply = (
                        command,
                        {"message": _encode(result), "tokens": tokens_used([result])},
                        None,
                    )
                else:
                    call = command.payload
                    if call["name"] in GATEWAY_WRITES and run.spec.contract.has(
                        Capability.IDEMPOTENCY_KEY
                    ):
                        key = sha256(canonical_json([run.spec.run_id, worker, command.activity_id]))
                        call = {**call, "args": {**call["args"], "idempotency_key": key}}
                    try:
                        tool_result = await by_name[call["name"]].ainvoke(call)
                    except ValidationError as error:
                        tool_result = ToolMessage(
                            content=str(error),
                            name=call["name"],
                            tool_call_id=call["id"],
                            status="error",
                        )
                    if not isinstance(tool_result, ToolMessage):
                        raise self.infrastructure_error("the tool returned an unsupported reply")
                    reply = (command, _encode(tool_result), None)
            except TransportTimeout as error:
                reply = (command, None, str(error))

    async def _wait_for_handoff(self, worker: str, reply: Reply | None, *, start: bool) -> Delivery:
        raise NotImplementedError


def _encode(message: BaseMessage) -> dict[str, Any]:
    return {"type": message.type, "data": message.model_dump(mode="json", exclude={"id"})}
