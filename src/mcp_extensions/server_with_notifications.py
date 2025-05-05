from typing import Any, AsyncGenerator, Optional
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio import create_memory_object_stream
from datetime import timedelta
import json
import asyncio

from agents.mcp.server import MCPServerSse, MCPServerSseParams
from mcp.client.session import ClientSession
from mcp.types import JSONRPCMessage
from httpx_sse._models import ServerSentEvent


class ClientSessionWithLoggingNotifications(ClientSession):
    """ClientSession that forwards notifications to the logging stream."""

    def __init__(
        self,
        read_stream: MemoryObjectReceiveStream[JSONRPCMessage | Exception],
        write_stream: MemoryObjectSendStream[JSONRPCMessage],
        logging_send_stream: MemoryObjectSendStream[dict[str, Any]],
        timeout: Optional[timedelta] = None,
        *args,
        **kwargs,
    ):
        super().__init__(read_stream, write_stream, timeout, *args, **kwargs)
        self._logging_send_stream = logging_send_stream

    async def _received_notification(self, notification: Any) -> None:

        await super()._received_notification(notification)

        notification_dict = notification.model_dump()

        if notification_dict.get("method", "").startswith("notifications/"):
            await self._logging_send_stream.send(notification_dict)


class MCPServerSseWithNotifications(MCPServerSse):
    """MCP Server that reads normal + logging notifications."""

    def __init__(
        self,
        params: MCPServerSseParams,
        cache_tools_list: bool = False,
        name: Optional[str] = None,
        client_session_timeout_seconds: Optional[float] = 5,
    ):
        super().__init__(params, cache_tools_list, name, client_session_timeout_seconds)

        self._notification_read_stream: Optional[MemoryObjectReceiveStream[JSONRPCMessage | Exception | ServerSentEvent]] = None
        self._logging_send_stream, self._logging_read_stream = create_memory_object_stream(0)

    async def connect(self):
        await super().connect()

        if self.session is not None:
            self.session._logging_callback = self._handle_logging_notification

        streams = self.create_streams()
        async with streams as (read, _):
            self._notification_read_stream = read

    async def cleanup(self):
        await super().cleanup()

        try:
            await self._logging_send_stream.aclose()
        except Exception:
            pass

        try:
            await self._logging_read_stream.aclose()
        except Exception:
            pass

        try:
            if self._notification_read_stream is not None:
                await self._notification_read_stream.aclose()
        except Exception:
            pass

    async def stream_notifications(self) -> AsyncGenerator[dict[str, Any], None]:
        """Yield notifications from both server and logging."""
        if self._notification_read_stream is None:
            raise RuntimeError("Not connected")

        server_stream = self._notification_read_stream.__aiter__()
        logging_stream = self._logging_read_stream.__aiter__()

        try:
            while True:
                if server_stream is None and logging_stream is None:
                    yield {"method": "notifications/stream_end"}
                    return

                if server_stream is not None:
                    try:
                        message = await asyncio.wait_for(server_stream.__anext__(), timeout=0.1)

                        if isinstance(message, Exception):
                            raise message

                        if isinstance(message, ServerSentEvent):
                            if message.event == "message":
                                try:
                                    data = json.loads(message.data)
                                    if data.get("method", "").startswith("notifications/"):
                                        yield data
                                except Exception:
                                    pass
                            continue

                        message_dict = message.model_dump()
                        if message_dict.get("method", "").startswith("notifications/"):
                            yield message_dict

                    except asyncio.TimeoutError:
                        pass
                    except StopAsyncIteration:
                        server_stream = None

                if logging_stream is not None:
                    try:
                        log_notification = await asyncio.wait_for(logging_stream.__anext__(), timeout=0.01)
                        yield log_notification
                    except asyncio.TimeoutError:
                        pass
                    except StopAsyncIteration:
                        logging_stream = None

        finally:
            try:
                await self._logging_read_stream.aclose()
            except Exception:
                pass

            try:
                if self._notification_read_stream is not None:
                    await self._notification_read_stream.aclose()
            except Exception:
                pass

    async def _handle_logging_notification(self, params: Any):
        notification = {
            "method": "notifications/logging",
            "params": params.model_dump(),
        }
        await self._logging_send_stream.send(notification)

