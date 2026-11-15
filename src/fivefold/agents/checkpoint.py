"""The agent's checkpointer: LangGraph's Postgres saver, run inline (0008, 0009).

LangGraph keeps each worker's state in the run database's `agent` schema, where role
`ff_agent` has its search path (0008). A worker killed by F11 resumes from its last
checkpoint there.

The async Postgres saver does socket I/O that the deterministic scheduler cannot see,
so the run would go idle while a query is in flight. `InlinePostgresSaver` is the sync
saver with async methods that call the sync ones directly: each checkpoint write is part
of the agent's step, as every other database call in a run is (0009).

Checked in langgraph-checkpoint-postgres 3.2.1 and langgraph-checkpoint 4.3.2: the sync
`PostgresSaver` takes a psycopg connection made with `autocommit=True`,
`prepare_threshold=0` and `row_factory=dict_row` (as its `from_conn_string` makes one),
and `setup()` creates its tables; the base saver's async methods raise
`NotImplementedError` until a subclass provides them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import DictRow, dict_row


def connect(conninfo: str) -> psycopg.Connection[DictRow]:
    """A connection as the saver needs it."""
    return psycopg.connect(conninfo, autocommit=True, prepare_threshold=0, row_factory=dict_row)


class InlinePostgresSaver(PostgresSaver):
    """The sync Postgres saver, with async methods that run it inline."""

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)
