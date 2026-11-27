"""Hand B1 Activities to FiveFold without advancing virtual time while Temporal runs."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fivefold._temporal_cli import VERSION as CLI_VERSION
from fivefold._temporal_cli import verify_executable
from fivefold.harness.baseline_worker import (
    BaselineInfrastructureError,
    BaselineWorkers,
    Broken,
    Reply,
    _encode,
)
from fivefold.harness.baseline_worker import (
    Delivery as Delivery,
)
from fivefold.harness.baseline_worker import (
    Done as Done,
)
from fivefold.harness.baseline_worker import (
    Request as Request,
)
from fivefold.harness.temporal_workflow import ScriptedWorkflow

if TYPE_CHECKING:
    from fivefold.harness.run import InprocRun

TASK_QUEUE = "fivefold-b1"
CLI_HELP = (
    f"Temporal CLI {CLI_VERSION} is required. Run make temporal-setup or set "
    "FIVEFOLD_TEMPORAL_CLI to the pinned binary."
)
HOST_WAIT_S = 30
# Record 0018 keeps Temporal's default Activity policy. Add its largest backoff
# to the ordinary host wait so failed attempts get the same scheduling margin.
ACTIVITY_RETRY_BACKOFF_CAP_S = 100
TIMEOUT_EVENTS = (
    EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT,
    EventType.EVENT_TYPE_WORKFLOW_TASK_TIMED_OUT,
    EventType.EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT,
)


class TemporalInfrastructureError(BaselineInfrastructureError):
    """Temporal infrastructure failed before the episode could be scored."""


def temporal_cli_path() -> Path:
    path = Path(os.environ.get("FIVEFOLD_TEMPORAL_CLI", ".temporal/temporal")).resolve()
    if not path.is_file():
        raise TemporalInfrastructureError(CLI_HELP)
    try:
        verify_executable(path)
    except OSError:
        raise TemporalInfrastructureError(CLI_HELP) from None
    except ValueError as error:
        raise TemporalInfrastructureError(f"{CLI_HELP} ({error})") from None
    return path


def check_temporal_cli() -> None:
    """Check the pinned binary without starting a Temporal server."""
    path = temporal_cli_path()
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise TemporalInfrastructureError(CLI_HELP) from None
    if result.returncode != 0 or result.stdout.split()[:3] != ["temporal", "version", CLI_VERSION]:
        raise TemporalInfrastructureError(CLI_HELP)


Receiver = Callable[[Delivery], None]


class TemporalHost:
    """A loopback dev server and poller on a clock separate from FiveFold's."""

    def __init__(self) -> None:
        self.cli = str(temporal_cli_path())
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
        self.workflows: dict[str, tuple[asyncio.Task[bool], asyncio.Event]] = {}

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
                    for receive in tuple(self.receivers.values()):
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
                try:
                    await self.stop.wait()
                finally:
                    await self._finish_workflows(tuple(self.workflows))

    def next_episode(self) -> str:
        self.episode += 1
        return f"{self.task_queue}-{self.episode}"

    def start(self, execution: str, spec: dict[str, Any], receive: Receiver) -> None:
        async def workflow_result(started: asyncio.Event) -> bool:
            self.receivers[execution] = receive
            try:
                try:
                    handle = await self.client.start_workflow(
                        ScriptedWorkflow.run,
                        spec,
                        id=execution,
                        task_queue=self.task_queue,
                        rpc_timeout=timedelta(seconds=HOST_WAIT_S),
                    )
                finally:
                    started.set()
                result = await handle.result()
                history = await handle.fetch_history()
                if any(event.event_type in TIMEOUT_EVENTS for event in history.events):
                    raise TemporalInfrastructureError(
                        "Temporal history contains an infrastructure timeout"
                    )
                receive(Done(result))
                return True
            except Exception as error:
                receive(Broken(error))
                return False
            finally:
                self.receivers.pop(execution, None)

        def schedule() -> None:
            started = asyncio.Event()
            self.workflows[execution] = (asyncio.create_task(workflow_result(started)), started)

        if self.loop.is_closed():
            raise TemporalInfrastructureError("Temporal poller is closed")
        try:
            self.loop.call_soon_threadsafe(schedule)
        except RuntimeError as error:
            raise TemporalInfrastructureError(
                "Temporal poller could not start the workflow"
            ) from error

    async def finish(self, executions: tuple[str, ...]) -> None:
        if executions and not self.loop.is_closed():
            await self._submit(self._finish_workflows(executions))

    async def _finish_workflows(self, executions: tuple[str, ...]) -> None:
        async def finish(execution: str) -> None:
            tracked = self.workflows.get(execution)
            if tracked is None:
                return
            task, started = tracked
            try:
                await started.wait()
                if not task.done() or not task.result():
                    await self.client.get_workflow_handle(execution).terminate(
                        reason="FiveFold episode ended",
                        rpc_timeout=timedelta(seconds=HOST_WAIT_S),
                    )
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.workflows.pop(execution, None)

        await asyncio.gather(*(finish(execution) for execution in executions))

    async def _submit(self, operation: Coroutine[Any, Any, None]) -> None:
        try:
            if self.loop.is_closed():
                raise RuntimeError("Temporal poller is closed")
            future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        except RuntimeError as error:
            operation.close()
            raise TemporalInfrastructureError("Temporal poller is unavailable") from error
        await asyncio.wrap_future(future)

    async def answer(self, request: Request, result: Any, error: str | None) -> None:
        async def reply() -> None:
            handle = self.client.get_async_activity_handle(task_token=request.task_token)
            if error is None:
                await handle.complete(result)
            else:
                await handle.fail(ApplicationError(error))

        await self._submit(reply())

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


class TemporalWorkers(BaselineWorkers):
    """Per-run Activity handoffs. The scheduler owns all simulator and model work."""

    def __init__(self, run: InprocRun, host: TemporalHost) -> None:
        super().__init__(
            run, TemporalInfrastructureError, "Temporal workflow failed outside virtual I/O"
        )
        self.host = host
        self.episode = host.next_episode()
        self.queues: dict[str, asyncio.Queue[Delivery]] = {}
        self.executions: set[str] = set()

    async def stop(self) -> None:
        """End this episode's workflows before releasing the host or reusing it."""
        await self.host.finish(tuple(sorted(self.executions)))

    async def work(self, worker: str, *, resume: bool) -> None:
        if not resume:
            self.queues[worker] = asyncio.Queue()
        await super().work(worker, resume=resume)

    async def _wait_for_handoff(self, worker: str, reply: Reply | None, *, start: bool) -> Delivery:
        retry = reply is not None and reply[2] is not None
        limit = HOST_WAIT_S + ACTIVITY_RETRY_BACKOFF_CAP_S if retry else HOST_WAIT_S
        # Keep handoffs in virtual scheduler order. A worker queued behind another
        # worker's retry starts its deadline only when it owns the handoff.
        try:
            async with self.handoff:
                return await asyncio.wait_for(self._next(worker, reply, start=start), timeout=limit)
        except TimeoutError:
            if reply is None:
                activity = "waiting for Activity model-1 attempt 1"
            else:
                pending = reply[0]
                if retry:
                    activity = (
                        f"waiting for Activity {pending.activity_id} attempt {pending.attempt + 1}"
                    )
                else:
                    activity = f"after Activity {pending.activity_id} attempt {pending.attempt}"
            raise TemporalInfrastructureError(f"Temporal handoff timed out {activity}") from None

    async def _next(self, worker: str, reply: Reply | None, *, start: bool) -> Delivery:
        queue = self.queues[worker]
        if start:
            loop = asyncio.get_running_loop()

            def deliver(command: Delivery) -> None:
                if loop.is_closed():
                    return
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, command)
                except RuntimeError:
                    if not loop.is_closed():
                        raise

            execution = f"{self.episode}-{worker}"
            self.executions.add(execution)
            self.host.start(
                execution,
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
