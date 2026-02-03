"""Forum Memory - Cross-session memory storage for AstrBook activities.

This module provides a shared memory storage that can be accessed
from any session (QQ, Telegram, etc.) to recall the bot's forum activities.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrbot import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


@dataclass
class MemoryItem:
    """A single memory item."""

    memory_type: str
    """Type of memory: browsed, mentioned, replied, new_thread, etc."""

    content: str
    """Human-readable description of the memory."""

    timestamp: datetime = field(default_factory=datetime.now)
    """When this memory was created."""

    metadata: dict = field(default_factory=dict)
    """Additional metadata (thread_id, user info, etc.)."""

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "memory_type": self.memory_type,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryItem":
        """Create from dictionary."""
        return cls(
            memory_type=data["memory_type"],
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {}),
        )


class ForumMemory:
    """Shared memory storage for forum activities.

    This class stores the bot's forum activities (browsing, replying, etc.)
    in a way that can be accessed from any session through LLM tools.

    Features:
    - Automatic persistence to disk
    - Memory limit to prevent unbounded growth
    - Human-readable summaries for LLM consumption
    - Type-based filtering
    """

    def __init__(self, max_items: int = 50):
        """Initialize forum memory.

        Args:
            max_items: Maximum number of memory items to keep.
        """
        self._max_items = max_items
        self._memories: list[MemoryItem] = []
        self._storage_path = os.path.join(
            get_astrbot_data_path(),
            "astrbook",
            "forum_memory.json",
        )

        # Ensure directory exists
        os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)

        # Load existing memories
        self._load()

    def add_memory(
        self,
        memory_type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ):
        """Add a new memory item.

        Args:
            memory_type: Type of memory (browsed, mentioned, replied, etc.)
            content: Human-readable description
            metadata: Additional metadata
        """
        item = MemoryItem(
            memory_type=memory_type,
            content=content,
            metadata=metadata or {},
        )
        self._memories.append(item)

        # Trim if exceeds limit
        if len(self._memories) > self._max_items:
            self._memories = self._memories[-self._max_items:]

        # Persist to disk
        self._save()

        logger.debug(f"[ForumMemory] Added: [{memory_type}] {content[:50]}...")

    def get_memories(
        self,
        memory_type: str | None = None,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        """Get memory items, optionally filtered by type.

        Args:
            memory_type: Filter by memory type (optional)
            limit: Maximum number of items to return (optional)

        Returns:
            List of memory items, newest first.
        """
        items = self._memories.copy()

        if memory_type:
            items = [m for m in items if m.memory_type == memory_type]

        # Return newest first
        items = items[::-1]

        if limit:
            items = items[:limit]

        return items

    def get_summary(self, limit: int = 10) -> str:
        """Get a human-readable summary of recent activities.

        This is designed to be consumed by LLM for cross-session recall.

        Args:
            limit: Maximum number of items to include

        Returns:
            Formatted summary string.
        """
        items = self.get_memories(limit=limit)

        if not items:
            return "最近没有论坛活动记录。"

        lines = ["我最近在 AstrBook 论坛的活动："]

        for item in items:
            time_str = item.timestamp.strftime("%m-%d %H:%M")
            type_emoji = self._get_type_emoji(item.memory_type)
            lines.append(f"  {type_emoji} [{time_str}] {item.content}")

        return "\n".join(lines)

    def get_context_for_thread(self, thread_id: int) -> str:
        """Get memories related to a specific thread.

        Args:
            thread_id: The thread ID to filter by.

        Returns:
            Formatted context string.
        """
        items = [m for m in self._memories if m.metadata.get("thread_id") == thread_id]

        if not items:
            return f"没有与帖子 #{thread_id} 相关的活动记录。"

        lines = [f"与帖子 #{thread_id} 相关的活动："]
        for item in items[-5:]:  # Last 5 activities
            time_str = item.timestamp.strftime("%m-%d %H:%M")
            lines.append(f"  - [{time_str}] {item.content}")

        return "\n".join(lines)

    def clear(self):
        """Clear all memories."""
        self._memories.clear()
        self._save()
        logger.info("[ForumMemory] Cleared all memories")

    def _get_type_emoji(self, memory_type: str) -> str:
        """Get emoji for memory type."""
        emojis = {
            "browsed": "👀",
            "mentioned": "📢",
            "replied": "💬",
            "new_thread": "📝",
            "created": "✍️",
            "diary": "📔",  # Agent's personal diary
        }
        return emojis.get(memory_type, "📌")

    def _load(self):
        """Load memories from disk."""
        if not os.path.exists(self._storage_path):
            return

        try:
            with open(self._storage_path, encoding="utf-8") as f:
                data = json.load(f)

            self._memories = [MemoryItem.from_dict(d) for d in data]
            logger.debug(f"[ForumMemory] Loaded {len(self._memories)} memories")
        except Exception as e:
            logger.error(f"[ForumMemory] Failed to load: {e}")
            self._memories = []

    def _save(self):
        """Save memories to disk."""
        try:
            data = [m.to_dict() for m in self._memories]
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[ForumMemory] Failed to save: {e}")

    def __len__(self) -> int:
        """Get number of memories."""
        return len(self._memories)
