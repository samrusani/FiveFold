"""The action gateway (0001, 0002, 0003, 0005).

The MCP server and admin API, the action state machine, the dispatcher and startup
recovery. The gateway reaches the simulator only through the simulator's API, and never
imports `fivefold.sim`.
"""
