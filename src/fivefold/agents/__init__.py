"""The agent runner (0008, 0009).

The shared LangGraph agent, the model adapters (scripted, live, replay), the scripted
policy and the baseline tool clients. Agents reach tools through MCP in gateway
configurations, and through the baseline client in baselines. They never import
`fivefold.gateway` or `fivefold.sim`.
"""
