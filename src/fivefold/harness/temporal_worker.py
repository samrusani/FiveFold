"""Hand B1 Activities to FiveFold without advancing virtual time while Temporal runs."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    messages_from_dict,
)
from pydantic import ValidationError
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fivefold.agents.tools import GATEWAY_WRITES, baseline_tools
from fivefold.agents.worker import WorkerEnd, tokens_used
from fivefold.domain.contracts import Capability
from fivefold.harness.temporal_workflow import ScriptedWorkflow
from fivefold.harness.transport import TransportTimeout
from fivefold.recorder.cassette import canonical_json, sha256

if TYPE_CHECKING:
    from fivefold.harness.run import InprocRun

TASK_QUEUE = "fivefold-b1"
HOST_WAIT_S = 30
TIMEOUT_EVENTS = (
    EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT,
    EventType.EVENT_TYPE_WORKFLOW_TASK_TIMED_OUT,
    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
)


class TemporalInfrastructureError(RuntimeError):
    """Temporal infrastructure failed before the episode could be scored."""


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
Receiver = Callable[[Delivery], None]
Reply = tuple[Request, Any, str | None]


class TemporalHost:
    """A loopback dev server and poller on a clock separate from FiveFold's."""

    def __init__(self) -> None:
        path = Path(os.environ.get("FIVEFOLD_TEMPORAL_CLI", ".temporal/temporal")).resolve()
        if not path.is_file():
            raise TemporalInfrastructureError(
                "The local Temporal CLI is required. Run make temporal-setup or set "
                "FIVEFOLD_TEMPORAL_CLI to the pinned binary."
            )
        self.cli = str(path)
        self.task_queue = f"{TASK_QUEUE}-{os.getpid()}"
        self.before_activity: Callable[[str, int], Awaitable[None]] | None = None
        self.loop = asyncio.new_event_loop()
        self.stop = asyncio.Event()
        self.ready: Future[None] = Future()
        self.thread = threading.Thread(
            target=self._serve, name="FiveFold Temporal poller", daemon=True
        )
        self.episode = 0
        self.receivers: dict[str, Receiver] = {}

    def __enter__(self) -> TemporalHost:
        self.thread.start()
        try:
            self.ready.result(timeout=HOST_WAIT_S)
        except BaseException:
            self._stop()
            raise
        return self

    def __exit__(self, *args: object) -> None:
        self._stop()

    def _stop(self) -> None:
        if not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.stop.set)
            except RuntimeError:
                # The poller can close its loop between the check and scheduling.
                if not self.loop.is_closed():
                    raise
        self.thread.join(timeout=HOST_WAIT_S)
        if self.thread.is_alive():
            raise TemporalInfrastructureError("Temporal host did not stop")

    def _serve(self) -> None:
        with asyncio.Runner(loop_factory=lambda: self.loop) as runner:
            try:
                runner.run(self._poll())
            except BaseException as error:
                if not self.ready.done():
                    self.ready.set_exception(error)
                else:
                    logging.exception("Temporal poller stopped")
                    for receive in self.receivers.values():
                        receive(Broken(error))

    async def _poll(self) -> None:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=self.cli,
            dev_server_startup_timeout=timedelta(seconds=20),
        ) as environment:
            self.client = environment.client
            async with Worker(
                self.client,
                task_queue=self.task_queue,
                workflows=[ScriptedWorkflow],
                activities=[self.model, self.tool, self.report],
                max_cached_workflows=0,
            ):
                self.ready.set_result(None)
                await self.stop.wait()

    def next_episode(self) -> str:
        self.episode += 1
        return f"{self.task_queue}-{self.episode}"

    def start(self, execution: str, spec: dict[str, Any], receive: Receiver) -> None:
        async def workflow_result() -> None:
            self.receivers[execution] = receive
            try:
                handle = await self.client.start_workflow(
                    ScriptedWorkflow.run, spec, id=execution, task_queue=self.task_queue
                )
                result = await handle.result()
                history = await handle.fetch_history()
                if any(event.event_type in TIMEOUT_EVENTS for event in history.events):
                    raise TemporalInfrastructureError(
                        "Temporal history contains an infrastructure timeout"
                    )
                receive(Done(result))
            except Exception as error:
                receive(Broken(error))
            finally:
                self.receivers.pop(execution, None)

        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(workflow_result()))

    async def answer(self, request: Request, result: Any, error: str | None) -> None:
        async def reply() -> None:
            handle = self.client.get_async_activity_handle(task_token=request.task_token)
            if error is None:
                await handle.complete(result)
            else:
                await handle.fail(ApplicationError(error))

        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(reply(), self.loop))

    async def _detach(self, kind: str, payload: dict[str, Any]) -> None:
        info = activity.info()
        if self.before_activity is not None:
            await self.before_activity(info.activity_id, info.attempt)
        if info.workflow_id is None:
            raise TemporalInfrastructureError("A B1 Activity must belong to a Workflow")
        self.receivers[info.workflow_id](
            Request(kind, info.activity_id, info.attempt, info.task_token, payload)
        )
        activity.raise_complete_async()

    @activity.defn(name="b1_model")
    async def model(self, payload: dict[str, Any]) -> None:
        await self._detach("model", payload)

    @activity.defn(name="b1_tool")
    async def tool(self, payload: dict[str, Any]) -> None:
        await self._detach("tool", payload)

    @activity.defn(name="b1_report")
    async def report(self, payload: dict[str, Any]) -> None:
        await self._detach("report", payload)


class TemporalWorkers:
    """Per-run Activity handoffs. The scheduler owns all simulator and model work."""

    def __init__(self, run: InprocRun, host: TemporalHost) -> None:
        self.run = run
        self.host = host
        self.episode = host.next_episode()
        self.queues: dict[str, asyncio.Queue[Delivery]] = {}
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
        else:
            self.queues[worker] = asyncio.Queue()

        while True:
            command = await run.scheduler.hold(
                asyncio.wait_for(
                    self._next(worker, reply, start=not resume and reply is None),
                    timeout=HOST_WAIT_S,
                )
            )
            if isinstance(command, Done):
                run.result.ends[worker] = (WorkerEnd(command.result[0]), command.result[1])
                return
            if isinstance(command, Broken):
                raise TemporalInfrastructureError(
                    "Temporal workflow failed outside virtual I/O"
                ) from command.error

            self.active[worker] = command
            try:
                if command.kind == "model":
                    asked = run._call_log(worker)()
                    messages = messages_from_dict(command.payload["messages"])
                    result = await bound.ainvoke(messages)
                    if not isinstance(result, AIMessage) or len(result.tool_calls) > 1:
                        raise TemporalInfrastructureError(
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
                        raise TemporalInfrastructureError("the tool returned an unsupported reply")
                    reply = (command, _encode(tool_result), None)
            except TransportTimeout as error:
                reply = (command, None, str(error))

    async def _next(self, worker: str, reply: Reply | None, *, start: bool) -> Delivery:
        queue = self.queues[worker]
        # Reserve the next requested virtual operation before a second worker can
        # deliver its own handoff. Release the lock before any virtual wait.
        async with self.handoff:
            if start:
                loop = asyncio.get_running_loop()

                def deliver(command: Delivery) -> None:
                    loop.call_soon_threadsafe(queue.put_nowait, command)

                self.host.start(
                    f"{self.episode}-{worker}",
                    {
                        "initial": _encode(HumanMessage(self.run.spec.scenario.task)),
                        "max_steps": self.run.spec.scenario.budget.max_agent_steps,
                        "max_tokens": self.run.spec.scenario.budget.max_tokens,
                    },
                    deliver,
                )
            if reply is not None:
                pending, result, error = reply
                await self.host.answer(pending, result, error)
                self.active.pop(worker)
            following = await queue.get()
            if reply is not None and reply[2] is not None:
                pending = reply[0]
                if not isinstance(following, Request) or (
                    following.activity_id != pending.activity_id
                    or following.attempt != pending.attempt + 1
                ):
                    raise TemporalInfrastructureError(
                        "retry departed from the virtual failure sequence"
                    )
            elif isinstance(following, Request) and (
                following.attempt != 1
                or (reply is not None and following.activity_id == reply[0].activity_id)
            ):
                raise TemporalInfrastructureError("unexpected activity retry")
            return following


def _encode(message: BaseMessage) -> dict[str, Any]:
    return {"type": message.type, "data": message.model_dump(mode="json", exclude={"id"})}
