"""Read a checkpoint through the serializer selected by the in-process runner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import ChannelVersions, empty_checkpoint
from psycopg.conninfo import make_conninfo

from fivefold.agents.checkpoint import InlinePostgresSaver, connect
from fivefold.harness.demo import demo_1_spec
from fivefold.harness.run import InprocRun

pytestmark = pytest.mark.pg


@dataclass
class UnlistedNote:
    """A plain test value that no FiveFold checkpoint needs to construct."""

    text: str


def test_checkpoint_restore_keeps_unlisted_values_as_data(
    run_database: Callable[[str], str],
) -> None:
    dsn = run_database("strict_checkpoint")
    run = InprocRun(demo_1_spec("G2", 7), dsn)
    asyncio.run(run.execute())

    with connect(make_conninfo(dsn, user="ff_agent")) as conn:
        saver = InlinePostgresSaver(conn, serde=run.checkpointer.serde)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {
            "probe": UnlistedNote("test message"),
            "messages": [HumanMessage("hello")],
        }
        versions: ChannelVersions = {"probe": 1, "messages": 1}
        checkpoint["channel_versions"] = versions
        saved = saver.put(
            {"configurable": {"thread_id": "restore-test", "checkpoint_ns": ""}},
            checkpoint,
            {"source": "input", "step": -1, "parents": {}},
            versions,
        )
        restored = asyncio.run(saver.aget_tuple(saved))

    assert restored is not None
    assert restored.checkpoint["channel_values"] == {
        "probe": {"text": "test message"},
        "messages": [HumanMessage("hello")],
    }
