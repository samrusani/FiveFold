"""The durable scripted conversation for a B1 worker (0018)."""

from datetime import timedelta
from typing import Any

from temporalio import workflow

# Infrastructure guard only. Every application timeout uses FiveFold virtual time.
START_TO_CLOSE = timedelta(minutes=10)


@workflow.defn
class ScriptedWorkflow:
    @workflow.run
    async def run(self, spec: dict[str, Any]) -> list[str]:
        messages = [spec["initial"]]
        steps = tokens = 0
        while steps < spec["max_steps"]:
            if spec["max_tokens"] is not None and tokens >= spec["max_tokens"]:
                return ["budget", "Token budget used up."]

            steps += 1
            reply = await workflow.execute_activity(
                "b1_model",
                {"step": steps, "messages": messages},
                activity_id=f"model-{steps}",
                start_to_close_timeout=START_TO_CLOSE,
            )
            messages.append(reply["message"])
            tokens += reply["tokens"]
            calls = reply["message"]["data"]["tool_calls"]
            if not calls:
                return ["finished", reply["message"]["data"]["content"]]
            call = calls[0]
            tool_result = await workflow.execute_activity(
                "b1_report" if call["name"] == "submit_final_report" else "b1_tool",
                call,
                activity_id=f"tool-{steps}",
                start_to_close_timeout=START_TO_CLOSE,
            )
            messages.append(tool_result)

        return ["budget", "Step budget used up."]
