# mcp_extensions/streamable_agent_stream.py
import asyncio
from typing import Any, AsyncGenerator, List

from openai.types.responses import (
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseOutputItemAddedEvent,
    ResponseContentPartAddedEvent,
    ResponseTextDeltaEvent,
    ResponseContentPartDoneEvent,
)

from agents.items import MessageOutputItem
from agents.stream_events import RunItemStreamEvent

from .server_with_notifications import MCPServerSseWithNotifications


class StreamableAgentStream:
    """
    Multiplex an OpenAI‑Agents streamed run with server‑side notifications so
    callers see a single unified event stream.

    * Agent tool‑call events arrive from the normal agent stream.
    * SSE / logging notifications arrive from an MCPServerSseWithNotifications
      instance and are converted into “assistant message” events **and** added
      to the run‑history so the next LLM turn can reference them.
    """

    # --------------------------------------------------------------------- #
    # construction                                                          #
    # --------------------------------------------------------------------- #
    def __init__(self, base_stream, mcp_server: MCPServerSseWithNotifications):
        self.base_stream = base_stream
        self.mcp_server = mcp_server

        # state for synthesised assistant‑message built from notifications
        self.notification_started: bool = False
        self.notification_content_index: int = 0
        self.notification_message_id: str = "stream_notification"

        # buffer of notification text we’ve already streamed (for flush later)
        self._notification_buffer: List[str] = []

    # --------------------------------------------------------------------- #
    # public API                                                            #
    # --------------------------------------------------------------------- #
    async def stream_events(self) -> AsyncGenerator[Any, None]:
        """
        Yield events coming from both:
          • the standard Agent streaming run, and
          • the server‑side notification stream.

        When the notification stream completes (or is cancelled after a period
        of inactivity) any accumulated text is committed to the Agent
        run‑history so the model can “see” it on the next turn.
        """
        agent_stream = self.base_stream.stream_events()
        notification_stream = self.mcp_server.stream_notifications()

        agent_task = asyncio.create_task(agent_stream.__anext__())
        notif_task = asyncio.create_task(notification_stream.__anext__())

        agent_done = False
        notif_done = False
        idle_notification_counter = 0  # counts 0.1‑second ticks after agent done

        while True:
            active_tasks = [t for t in (agent_task, notif_task) if t is not None]
            if not active_tasks:
                break

            done, _ = await asyncio.wait(
                active_tasks,
                timeout=0.1,                     # regain control regularly
                return_when=asyncio.FIRST_COMPLETED,
            )

            # ------------------------------------------------------------------
            # timeout – give notifications a brief window after agent finishes
            # ------------------------------------------------------------------
            if not done:
                if agent_done and notif_task is not None:
                    idle_notification_counter += 1
                    if idle_notification_counter >= 5:  # ≈ 0.5 s
                        # no more notifications → cancel the task and flush text
                        notif_task.cancel()
                        try:
                            await notif_task
                        except asyncio.CancelledError:
                            pass
                        self._finalise_notification_message()
                        notif_task = None
                        notif_done = True
                continue  # loop again

            # activity seen → reset idle counter
            idle_notification_counter = 0

            # ------------------------------------------------------------------
            # agent stream activity
            # ------------------------------------------------------------------
            if agent_task is not None and agent_task in done:
                try:
                    result = agent_task.result()
                    yield result
                    agent_task = asyncio.create_task(agent_stream.__anext__())
                except StopAsyncIteration:
                    agent_task = None
                    agent_done = True

            # ------------------------------------------------------------------
            # notification stream activity
            # ------------------------------------------------------------------
            if notif_task is not None and notif_task in done:
                try:
                    notification = notif_task.result()

                    async for event in self._notification_to_events(notification):
                        yield event

                    # schedule next notification chunk
                    notif_task = asyncio.create_task(notification_stream.__anext__())
                except StopAsyncIteration:
                    # notification stream finished → flush buffered text
                    self._finalise_notification_message()
                    notif_task = None
                    notif_done = True

            # exit once both streams are done
            if agent_done and notif_done:
                break

    # --------------------------------------------------------------------- #
    # helpers – convert notification → assistant message events             #
    # --------------------------------------------------------------------- #
    async def _notification_to_events(self, notification: dict[str, Any]):
        """
        Transform a single `notifications/*` JSON‑RPC payload into the minimal
        set of OpenAI streaming events required to render it as assistant
        output.  Handles simple plaintext notifications.
        """
        text_parts = self._extract_text_from_notification(notification)

        for text_part in text_parts:
            # first chunk → start a new assistant message
            if not self.notification_started:
                self.notification_started = True
                self.notification_content_index = 0

                assistant_item = ResponseOutputMessage(
                    id=self.notification_message_id,
                    content=[],
                    role="assistant",
                    status="in_progress",
                    type="message",
                )
                yield ResponseOutputItemAddedEvent(
                    item=assistant_item,
                    output_index=0,
                    type="response.output_item.added",
                )
                yield ResponseContentPartAddedEvent(
                    content_index=0,
                    item_id=self.notification_message_id,
                    output_index=0,
                    part=ResponseOutputText(text="", type="output_text", annotations=[]),
                    type="response.content_part.added",
                )

            # stream delta to UI
            yield ResponseTextDeltaEvent(
                content_index=self.notification_content_index,
                delta=text_part,
                item_id=self.notification_message_id,
                output_index=0,
                type="response.output_text.delta",
            )

            # keep a copy for final flush into history
            self._notification_buffer.append(text_part)

        # close the part if anything emitted
        if text_parts:
            yield ResponseContentPartDoneEvent(
                content_index=self.notification_content_index,
                item_id=self.notification_message_id,
                output_index=0,
                part=ResponseOutputText(
                    text="".join(text_parts),
                    type="output_text",
                    annotations=[],
                ),
                type="response.content_part.done",
            )

    # --------------------------------------------------------------------- #
    # helpers – flush buffered notification text into run‑history           #
    # --------------------------------------------------------------------- #
    def _finalise_notification_message(self) -> None:
        """
        Turn the buffered notification text into a completed
        `MessageOutputItem` and inject it into the run‑history so the next LLM
        turn can reference it.
        """
        if not self._notification_buffer:
            return

        full_text = "".join(self._notification_buffer)

        response_msg = ResponseOutputMessage(
            id=self.notification_message_id,
            content=[ResponseOutputText(text=full_text, type="output_text", annotations=[])],
            role="assistant",
            status="completed",
            type="message",
        )

        # create a MessageOutputItem pointing at the *current* agent
        message_item = MessageOutputItem(
            raw_item=response_msg,
            agent=self.base_stream.current_agent,
        )

        # 1️⃣  add to run‑history for prompt construction
        self.base_stream.new_items.append(message_item)

        # 2️⃣  emit semantic event so any listeners see it
        self.base_stream._event_queue.put_nowait(
            RunItemStreamEvent(
                name="message_output_created",
                item=message_item,
            )
        )

        # reset for any future notification stream
        self._notification_buffer.clear()
        self.notification_started = False

    # --------------------------------------------------------------------- #
    # static utils                                                          #
    # --------------------------------------------------------------------- #
    @staticmethod
    def _extract_text_from_notification(notification: dict[str, Any]) -> list[str]:
        """Return plaintext contained in a `notifications/*` payload."""
        params = notification.get("params", {})

        # Model A: assistant‑style content array
        content = params.get("content", [])
        parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if parts:
            return parts

        # Model B: generic `{type:"text", text:"…"}`
        data = params.get("data", {})
        if data.get("type") == "text" and data.get("text"):
            return [data["text"]]

        return []
