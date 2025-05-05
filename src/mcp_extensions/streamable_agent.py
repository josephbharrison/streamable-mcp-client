from agents import Agent, Runner
from .server_with_notifications import MCPServerSseWithNotifications
from .streamable_agent_stream import StreamableAgentStream


class StreamableAgent:
    def __init__(self, agent: Agent, mcp_server: MCPServerSseWithNotifications):
        self.agent = agent
        self.mcp_server = mcp_server

    def run_streamed(self, input: str):
        # This must use Runner to ensure proper run context
        base_stream = Runner.run_streamed(starting_agent=self.agent, input=input)
        return StreamableAgentStream(base_stream, self.mcp_server)
