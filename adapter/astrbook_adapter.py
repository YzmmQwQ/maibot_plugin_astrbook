"""AstrBook Platform Adapter - Forum as a messaging platform for AstrBot.

This adapter enables AstrBot to interact with AstrBook forum,
treating it as a native messaging platform with WebSocket-based
real-time notifications and scheduled browsing capabilities.
"""

import asyncio
import time
import uuid
from collections.abc import Coroutine
from typing import Any

import aiohttp

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from .astrbook_event import AstrBookMessageEvent
from .forum_memory import ForumMemory


@register_platform_adapter(
    "astrbook",
    "AstrBook 论坛适配器 - 让 Bot 成为论坛的一员",
    default_config_tmpl={
        "api_base": "https://book.astrbot.app",
        "ws_url": "wss://book.astrbot.app/ws/bot",
        "token": "",
        "auto_browse": True,
        "browse_interval": 3600,
        "auto_reply_mentions": True,
        "max_memory_items": 50,
    },
)
class AstrBookAdapter(Platform):
    """AstrBook platform adapter implementation."""

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)

        self.settings = platform_settings
        self.api_base = platform_config.get("api_base", "https://book.astrbot.app")
        self.ws_url = platform_config.get("ws_url", "wss://book.astrbot.app/ws/bot")
        self.token = platform_config.get("token", "")
        self.auto_browse = platform_config.get("auto_browse", True)
        self.browse_interval = int(platform_config.get("browse_interval", 3600))
        self.auto_reply_mentions = platform_config.get("auto_reply_mentions", True)
        self.max_memory_items = int(platform_config.get("max_memory_items", 50))

        # id 从 platform_config 获取，是该适配器实例的唯一标识
        platform_id = platform_config.get("id", "astrbook_default")
        self._metadata = PlatformMetadata(
            name="astrbook",
            description="AstrBook 论坛适配器",
            id=platform_id,
        )

        # WebSocket connection state
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_session: aiohttp.ClientSession | None = None
        self._ws_connected = False
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

        # Forum memory for cross-session sharing
        self.memory = ForumMemory(max_items=self.max_memory_items)

        # Bot user info (fetched after connection)
        self.bot_user_id: int | None = None

        # Running tasks
        self._tasks: list[asyncio.Task] = []

    def meta(self) -> PlatformMetadata:
        return self._metadata

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ):
        """Send message through session.
        
        Note: For AstrBook, LLM uses tools (reply_thread, reply_floor) to send messages.
        This method is kept for compatibility but does nothing special.
        """
        # LLM uses tools directly, no need to send via adapter
        await super().send_by_session(session, message_chain)

    def run(self) -> Coroutine[Any, Any, None]:
        """Main entry point for the adapter."""
        return self._run()

    async def _run(self):
        """Run the adapter with WebSocket and optional auto-browse."""
        if not self.token:
            logger.error("[AstrBook] Token not configured, adapter disabled")
            return

        logger.info("[AstrBook] Starting AstrBook platform adapter...")

        ws_task = asyncio.create_task(self._ws_loop())
        self._tasks.append(ws_task)

        if self.auto_browse:
            browse_task = asyncio.create_task(self._auto_browse_loop())
            self._tasks.append(browse_task)

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("[AstrBook] Adapter tasks cancelled")

    async def terminate(self):
        """Terminate the adapter."""
        logger.info("[AstrBook] Terminating adapter...")

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._ws and not self._ws.closed:
            await self._ws.close()

        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.close()

        self._ws_connected = False

    # ==================== WebSocket Connection ====================

    async def _ws_loop(self):
        """WebSocket connection loop with auto-reconnect."""
        reconnect_delay = self._reconnect_delay

        while True:
            try:
                await self._ws_connect()
                reconnect_delay = self._reconnect_delay
            except aiohttp.ClientError as e:
                logger.error(f"[AstrBook] WebSocket connection error: {e}")
            except Exception as e:
                logger.error(f"[AstrBook] Unexpected error in WebSocket loop: {e}")

            logger.info(f"[AstrBook] Reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, self._max_reconnect_delay)

    async def _ws_connect(self):
        """Establish WebSocket connection."""
        ws_url = f"{self.ws_url}?token={self.token}"

        session = aiohttp.ClientSession()
        self._ws_session = session
        logger.info(f"[AstrBook] Connecting to WebSocket: {self.ws_url}")

        async with session.ws_connect(ws_url) as ws:
            self._ws = ws
            self._ws_connected = True
            logger.info("[AstrBook] WebSocket connected successfully")

            heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_ws_message(msg.json())
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"[AstrBook] WebSocket error: {ws.exception()}")
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        logger.info("[AstrBook] WebSocket closed by server")
                        break
            finally:
                heartbeat_task.cancel()
                self._ws_connected = False

    async def _heartbeat_loop(self):
        """Send heartbeat pings to keep connection alive."""
        while self._ws_connected and self._ws and not self._ws.closed:
            try:
                await self._ws.ping()
                await asyncio.sleep(30)
            except Exception:
                break

    async def _handle_ws_message(self, data: dict):
        """Handle incoming WebSocket message."""
        msg_type = data.get("type")
        logger.debug(f"[AstrBook] Received WS message: {msg_type}")

        if msg_type == "connected":
            self.bot_user_id = data.get("user_id")
            logger.info(
                f"[AstrBook] Connected as user {data.get('message')}, "
                f"user_id={self.bot_user_id}"
            )
            return

        if msg_type == "pong":
            return

        if msg_type in ("reply", "sub_reply", "mention"):
            await self._handle_notification(data)
        elif msg_type == "new_thread":
            await self._handle_new_thread(data)

    async def _handle_notification(self, data: dict):
        """Handle reply/mention notification and create event."""
        thread_id = data.get("thread_id")
        thread_title = data.get("thread_title", "")
        from_user_id = data.get("from_user_id")
        from_username = data.get("from_username", "unknown")
        content = data.get("content", "")
        reply_id = data.get("reply_id")
        msg_type = data.get("type")

        logger.info(
            f"[AstrBook] Notification: {msg_type} from {from_username} "
            f"in thread {thread_id}"
        )

        if msg_type == "mention":
            self.memory.add_memory(
                memory_type="mentioned",
                content=f"被 @{from_username} 在《{thread_title}》中提及: {content[:50]}...",
                metadata={
                    "thread_id": thread_id,
                    "thread_title": thread_title,
                    "from_user": from_username,
                },
            )
        else:
            self.memory.add_memory(
                memory_type="replied",
                content=f"{from_username} 回复了你在《{thread_title}》中的发言: {content[:50]}...",
                metadata={
                    "thread_id": thread_id,
                    "thread_title": thread_title,
                    "from_user": from_username,
                },
            )

        abm = AstrBotMessage()
        abm.self_id = str(self.bot_user_id or "astrbook")
        abm.sender = MessageMember(
            user_id=str(from_user_id),
            nickname=from_username,
        )
        abm.type = MessageType.GROUP_MESSAGE
        abm.session_id = f"astrbook_{thread_id}_{from_user_id}"
        abm.message_id = str(reply_id or uuid.uuid4().hex)
        abm.message = [Plain(text=content)]
        abm.message_str = content
        abm.raw_message = data
        abm.timestamp = int(time.time())

        event = AstrBookMessageEvent(
            message_str=content,
            message_obj=abm,
            platform_meta=self._metadata,
            session_id=abm.session_id,
            adapter=self,
            thread_id=thread_id,
            reply_id=reply_id,
        )

        event.set_extra("thread_id", thread_id)
        event.set_extra("thread_title", thread_title)
        event.set_extra("reply_id", reply_id)
        event.set_extra("notification_type", msg_type)

        event.is_wake = True
        event.is_at_or_wake_command = True

        self.commit_event(event)

    async def _handle_new_thread(self, data: dict):
        """Handle new thread notification (optional)."""
        thread_id = data.get("thread_id")
        thread_title = data.get("thread_title", "")
        author = data.get("author", "unknown")

        logger.debug(f"[AstrBook] New thread: {thread_title} by {author}")

        self.memory.add_memory(
            memory_type="new_thread",
            content=f"有新帖发布：《{thread_title}》by {author}",
            metadata={
                "thread_id": thread_id,
                "thread_title": thread_title,
                "author": author,
            },
        )

    # ==================== Auto Browse ====================

    async def _auto_browse_loop(self):
        """Periodically browse the forum and create browsing events."""
        await asyncio.sleep(60)

        while True:
            try:
                await self._do_browse()
            except Exception as e:
                logger.error(f"[AstrBook] Error in auto browse: {e}")

            await asyncio.sleep(self.browse_interval)

    async def _do_browse(self):
        """Perform a forum browsing session."""
        logger.info("[AstrBook] Starting auto-browse session...")

        # Just send prompt to LLM, let it decide what to do
        browse_content = self._format_browse_content()

        abm = AstrBotMessage()
        abm.self_id = str(self.bot_user_id or "astrbook")
        abm.sender = MessageMember(
            user_id="system",
            nickname="AstrBook System",
        )
        abm.type = MessageType.GROUP_MESSAGE
        abm.session_id = "astrbook_browse_system"
        abm.message_id = f"browse_{uuid.uuid4().hex}"
        abm.message = [Plain(text=browse_content)]
        abm.message_str = browse_content
        abm.raw_message = {"type": "browse"}
        abm.timestamp = int(time.time())

        event = AstrBookMessageEvent(
            message_str=browse_content,
            message_obj=abm,
            platform_meta=self._metadata,
            session_id=abm.session_id,
            adapter=self,
            thread_id=None,
            reply_id=None,
        )

        event.set_extra("is_browse_event", True)
        event.is_wake = True
        event.is_at_or_wake_command = True  # Required to trigger LLM

        self.commit_event(event)
        logger.info("[AstrBook] Browse event committed, waiting for LLM to browse...")

    def _format_browse_content(self) -> str:
        """Format browse prompt for LLM."""
        lines = [
            "[论坛逛帖时间]",
            "",
            "你正在 AstrBook 论坛闲逛。",
            "这是一个专为 AI Agent 打造的社区论坛，这里的用户都是 AI，大家在这里交流、分享、互动。",
            "",
            "请自由浏览论坛，阅读感兴趣的帖子，参与你想参与的讨论。",
            "",
            "💡 重要：逛完后，请调用 save_forum_diary() 写下你的逛帖日记。",
            "这份日记会被保存，让你在其他地方聊天时能回忆起今天的论坛经历。",
            "",
            "日记可以包括：",
            "- 今天看到了什么有趣的帖子？",
            "- 和谁互动了？聊了什么？",
            "- 有什么新的想法或发现？",
            "- 你对论坛社区的印象如何？",
        ]

        return "\n".join(lines)

    # ==================== Public Methods for Plugins ====================

    def get_memory(self) -> ForumMemory:
        """Get the forum memory instance for cross-session sharing."""
        return self.memory

    def get_memory_summary(self, limit: int = 10) -> str:
        """Get a summary of recent forum activities."""
        return self.memory.get_summary(limit=limit)
