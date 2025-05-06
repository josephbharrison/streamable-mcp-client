#  src/mcp_extensions/streamable_agent_stream.py   (realtime‑relay ✓streaming)

"""
Realtime 'middle‑man' between an OpenAI‑Agents streamed run and an MCP
notifications/* SSE feed:

- Process each notification chunk
    - Surfaces to the UI as a normal delta event
    - Appended to RunResultStreaming.new_items immediately
- After appending advance the outer agent once and forward that event.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncGenerator

from openai.types.responses import (
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)

from agents import Runner
from agents.items import MessageOutputItem
from agents.stream_events import RunItemStreamEvent
from .server_with_notifications import MCPServerSseWithNotifications


_GRACE_TICKS = 5  # 5 × 0.1s of idle‑notification tolerance


# ════════════════════════════════════════════════════════════════════════════
class StreamableAgentStream:
    def __init__(self, base_stream, mcp_server: MCPServerSseWithNotifications):
        self._base_stream = base_stream  # RunResultStreaming
        self._mcp_server = mcp_server

        # State of the synthetic assistant message we build for the UI
        self._ui_msg_started = False
        self._ui_msg_id = "stream_notification"
        self._ui_content_index = 0

    async def stream_events(self) -> AsyncGenerator[Any, None]:
        agent_stream = self._base_stream.stream_events()
        notif_stream = self._mcp_server.stream_notifications()

        agent_task = asyncio.create_task(agent_stream.__anext__())
        notif_task = asyncio.create_task(notif_stream.__anext__())

        agent_done = notif_done = False
        idle_after_agent = 0

        while True:
            active = [t for t in (agent_task, notif_task) if t]
            if not active:
                break

            done, _ = await asyncio.wait(
                active, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
            )

            # Idle timeout after agent is finished
            if not done:
                if agent_done and notif_task:
                    idle_after_agent += 1
                    if idle_after_agent >= _GRACE_TICKS:
                        notif_task.cancel()
                        try:
                            await notif_task
                        except asyncio.CancelledError:
                            pass
                        notif_task = None
                        notif_done = True
                continue
            idle_after_agent = 0

            # Events from the agent run itself
            if agent_task and agent_task in done:
                try:
                    evt = agent_task.result()
                    yield evt
                    agent_task = asyncio.create_task(agent_stream.__anext__())
                except StopAsyncIteration:
                    agent_task = None
                    agent_done = True

            # Events from the MCP notification SSE stream
            if notif_task and notif_task in done:
                try:
                    notif = notif_task.result()

                    if notif.get("method") == "notifications/stream_end":
                        notif_task = None
                        notif_done = True
                    else:
                        async for ui_evt in self._handle_notification(notif):
                            yield ui_evt
                        notif_task = asyncio.create_task(notif_stream.__anext__())
                except StopAsyncIteration:
                    notif_task = None
                    notif_done = True

            if agent_done and notif_done:
                break

    async def _handle_notification(
        self, notif: dict[str, Any]
    ) -> AsyncGenerator[Any, None]:
        """Turn one notification payload into UI + history + agent‑advance."""
        for chunk in _extract_text_chunks(notif):

            # 1. Stream delta to UI
            if not self._ui_msg_started:
                self._ui_msg_started = True
                yield ResponseOutputItemAddedEvent(
                    item=ResponseOutputMessage(
                        id=self._ui_msg_id,
                        role="assistant",
                        type="message",
                        status="in_progress",
                        content=[],
                    ),
                    output_index=0,
                    type="response.output_item.added",
                )
                yield ResponseContentPartAddedEvent(
                    content_index=0,
                    item_id=self._ui_msg_id,
                    output_index=0,
                    part=ResponseOutputText(text="", type="output_text", annotations=[]),
                    type="response.content_part.added",
                )

            yield ResponseTextDeltaEvent(
                content_index=self._ui_content_index,
                delta=chunk,
                item_id=self._ui_msg_id,
                output_index=0,
                type="response.output_text.delta",
            )
            yield ResponseContentPartDoneEvent(
                content_index=self._ui_content_index,
                item_id=self._ui_msg_id,
                output_index=0,
                part=ResponseOutputText(text=chunk, type="output_text", annotations=[]),
                type="response.content_part.done",
            )
            self._ui_content_index += 1  # prepare index for next chunk

            # 2. Append chunk to RunResultStreaming.new_items
            msg_item = MessageOutputItem(
                raw_item=ResponseOutputMessage(
                    id=f"notif_{uuid.uuid4().hex}",
                    role="assistant",
                    type="message",
                    status="completed",
                    content=[
                        ResponseOutputText(text=chunk, type="output_text", annotations=[])
                    ],
                ),
                agent=self._base_stream.current_agent,
            )
            self._base_stream.new_items.append(msg_item)
            self._base_stream._event_queue.put_nowait(
                RunItemStreamEvent(name="message_output_created", item=msg_item)
            )

            # 3. Advance the outer agent once and surface that event
            next_evt = await Runner.continue_run(self._base_stream)
            if next_evt is not None:
                yield next_evt


# Helpers
def _extract_text_chunks(notification: dict[str, Any]) -> list[str]:
    """Pull plain‑text chunks out of a `notifications/*` JSON‑RPC payload."""
    params = notification.get("params", {})

    # Assistant‑style content array
    content = params.get("content", [])
    chunks = [c.get("text", "") for c in content if c.get("type") == "text"]
    if chunks:
        return chunks

    # Flat {type:"text", text:"…"}
    data = params.get("data", {})
    if data.get("type") == "text" and data.get("text"):
        return [data["text"]]

    return []
