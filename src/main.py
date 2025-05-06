# main.py  (diagnostic‑only version – NO functional changes)

import asyncio
import os
import subprocess
import time
from typing import Any
from enum import Enum

from agents import Agent, Runner, gen_trace_id, trace
from agents.model_settings import ModelSettings

from openai.types.responses import ResponseTextDeltaEvent
from mcp_extensions.server_with_notifications import MCPServerSseWithNotifications
from mcp_extensions.streamable_agent import StreamableAgent


# --- MCPServer modes ---
class MCPServerMode(Enum):
    PYTHON_SSE = "python sse"
    TYPESCRIPT_SSE = "typescript sse"
    STREAMABLE_HTTP = "typescript streamable_http"


# --- Main logic ---
async def run(mcp_server: MCPServerSseWithNotifications):
    agent = Agent(
        name="Assistant",
        instructions="Use the tools to answer the questions.",
        mcp_servers=[mcp_server],
        model_settings=ModelSettings(tool_choice="required"),
    )

    streamable_agent = StreamableAgent(agent, mcp_server)

    # --- Streaming tool call ---
    message = "stream up to 10 numbers."
    print(f"\n\nRunning: {message}")

    streamed_result = streamable_agent.run_streamed(input=message)

    async for event in streamed_result.stream_events():
        # relay‑injected notification chunk  (ResponseTextDeltaEvent directly)
        if isinstance(event, ResponseTextDeltaEvent):
            if event.delta:
                print(event.delta, end="", flush=True)

        # model‑originated chunk  (wrapped in RawResponsesStreamEvent)
        elif (
            getattr(event, "type", "") == "raw_response_event"
            and isinstance(getattr(event, "data", None), ResponseTextDeltaEvent)
            and event.data.delta
        ):
            print(event.data.delta, end="", flush=True)

    # --- Standard tool calls ---
    message = "Add these numbers: 7 and 22."
    print(f"\n\nRunning: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(result.final_output)

    message = "What's the weather in Tokyo?"
    print(f"\n\nRunning: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(result.final_output)

    message = "What's the secret word?"
    print(f"\n\nRunning: {message}")
    result = await Runner.run(starting_agent=agent, input=message)
    print(result.final_output)


def start_local_server() -> subprocess.Popen[Any]:
    """Start the local Python SSE server."""
    this_dir = os.path.dirname(os.path.abspath(__file__))
    server_file = os.path.join(this_dir, "streaming_server.py")

    print("Starting local SSE server at http://localhost:8000/sse ...")
    process = subprocess.Popen(["uv", "run", server_file])
    time.sleep(3)
    print("SSE server started.\n")
    return process


async def create_mcp_server(mode: MCPServerMode) -> MCPServerSseWithNotifications:
    if mode == MCPServerMode.PYTHON_SSE:
        return MCPServerSseWithNotifications(
            params={"url": "http://localhost:8000/sse"},
            name="SSE Server",
        )
    elif mode == MCPServerMode.TYPESCRIPT_SSE:
        return MCPServerSseWithNotifications(
            params={"url": "http://localhost:3000/sse"},
            name="SSE Server",
        )
    elif mode == MCPServerMode.STREAMABLE_HTTP:
        return MCPServerSseWithNotifications(
            params={"url": "http://localhost:3000/mcp"},
            name="Streamable HTTP Server",
        )


async def main(mode: MCPServerMode):
    server_instance = await create_mcp_server(mode)
    async with server_instance as server:
        trace_id = gen_trace_id()
        with trace(workflow_name="MCP Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
            await run(server)


if __name__ == "__main__":
    try:
        asyncio.run(main(mode=MCPServerMode.TYPESCRIPT_SSE))
    finally:
        exit(0)
