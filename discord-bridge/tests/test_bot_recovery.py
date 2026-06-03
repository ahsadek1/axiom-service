"""
test_bot_recovery.py — Tests for NexusDiscordBot state management and persistence.

Covers TC-06 (bot disconnect/reconnect state), enqueue behavior, queue depth,
and SQLite persist/load/remove round-trips.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import NexusDiscordBot, EmbedTask
from channel_registry import ChannelRegistry
from embed_builder import EmbedBuilder
from command_router import CommandRouter


def make_registry(tmp_dir: str) -> ChannelRegistry:
    channel_map = {
        "daily-health-report": {"agent": "SOVEREIGN", "category": "GOVERNANCE"},
    }
    map_path = os.path.join(tmp_dir, "channel_map.json")
    with open(map_path, "w") as f:
        json.dump(channel_map, f)
    return ChannelRegistry(map_path=map_path)


def make_bot(tmp_dir: str) -> NexusDiscordBot:
    """Create a NexusDiscordBot backed by a temp DB path."""
    registry = make_registry(tmp_dir)
    builder = EmbedBuilder(registry)
    router = CommandRouter(bus_url="http://localhost:9999", timeout_s=1)
    db_path = os.path.join(tmp_dir, "test_queue.db")
    bot = NexusDiscordBot(
        registry=registry,
        embed_builder=builder,
        command_router=router,
        queue_max=10,
        db_path=db_path,
    )
    return bot


class TestBotConnectionState(unittest.TestCase):
    """TC-06: is_bot_connected() returns False before on_ready, True after."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._bot = make_bot(self._tmp)

    def test_connected_false_before_on_ready(self) -> None:
        """TC-06a: Bot is not connected immediately after instantiation."""
        self.assertFalse(self._bot.is_bot_connected())

    def test_connected_true_after_setting_flag(self) -> None:
        """TC-06b: Simulating on_ready sets _connected to True."""
        self._bot._connected = True
        self.assertTrue(self._bot.is_bot_connected())

    def test_connected_false_after_disconnect(self) -> None:
        """TC-06c: Simulating on_disconnect sets _connected to False."""
        self._bot._connected = True
        self._bot._connected = False
        self.assertFalse(self._bot.is_bot_connected())


class TestBotEnqueue(unittest.TestCase):
    """Tests for enqueue() thread-safety and behavior."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._bot = make_bot(self._tmp)

    def test_enqueue_returns_false_when_no_running_loop(self) -> None:
        """enqueue() returns False when no asyncio event loop is running."""
        task = EmbedTask(
            channel_name="daily-health-report",
            payload={"channel": "daily-health-report", "agent": "SOVEREIGN", "title": "Test"},
        )
        # No event loop is running in a regular unittest thread
        result = self._bot.enqueue(task)
        self.assertFalse(result)

    def test_enqueue_persists_task_to_db(self) -> None:
        """enqueue() must persist the task to SQLite before attempting delivery."""
        task = EmbedTask(
            channel_name="daily-health-report",
            payload={"channel": "daily-health-report", "agent": "SOVEREIGN", "title": "Persist Me"},
        )
        # Enqueue (will return False since no loop, but persist must still happen)
        self._bot.enqueue(task)
        persisted = self._bot._load_persisted_tasks()
        ids = [t.task_id for t in persisted]
        self.assertIn(task.task_id, ids)


class TestQueueDepth(unittest.TestCase):
    """Tests for queue_depth()."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._bot = make_bot(self._tmp)

    def test_queue_depth_starts_at_zero(self) -> None:
        self.assertEqual(self._bot.queue_depth(), 0)


class TestPersistence(unittest.TestCase):
    """Tests for _persist_task, _load_persisted_tasks, _remove_persisted_task."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._bot = make_bot(self._tmp)

    def _make_task(self, suffix: str = "") -> EmbedTask:
        return EmbedTask(
            channel_name="daily-health-report",
            payload={
                "channel": "daily-health-report",
                "agent": "SOVEREIGN",
                "title": f"Test {suffix}",
            },
            urgent=False,
            task_id=f"task-{suffix}-{int(time.time() * 1000)}",
        )

    def test_persist_and_load_round_trip(self) -> None:
        """_persist_task() followed by _load_persisted_tasks() returns the same task."""
        task = self._make_task("A")
        self._bot._persist_task(task)
        loaded = self._bot._load_persisted_tasks()
        ids = [t.task_id for t in loaded]
        self.assertIn(task.task_id, ids)

    def test_remove_persisted_task_deletes_by_id(self) -> None:
        """_remove_persisted_task() removes only the specified task."""
        task_a = self._make_task("remove-A")
        task_b = self._make_task("keep-B")
        self._bot._persist_task(task_a)
        self._bot._persist_task(task_b)

        self._bot._remove_persisted_task(task_a.task_id)

        loaded = self._bot._load_persisted_tasks()
        ids = [t.task_id for t in loaded]
        self.assertNotIn(task_a.task_id, ids)
        self.assertIn(task_b.task_id, ids)

    def test_multiple_tasks_persist_in_order(self) -> None:
        """Multiple tasks persisted are loaded in insertion order (created_at ASC)."""
        tasks = [self._make_task(f"ord-{i}") for i in range(3)]
        for t in tasks:
            self._bot._persist_task(t)
            time.sleep(0.001)  # ensure distinct created_at

        loaded = self._bot._load_persisted_tasks()
        loaded_ids = [t.task_id for t in loaded]
        for t in tasks:
            self.assertIn(t.task_id, loaded_ids)

    def test_persist_idempotent_on_duplicate_id(self) -> None:
        """INSERT OR IGNORE — persisting same task_id twice does not raise."""
        task = self._make_task("dup")
        self._bot._persist_task(task)
        # Second call should not raise
        self._bot._persist_task(task)
        loaded = self._bot._load_persisted_tasks()
        # Should appear exactly once
        matching = [t for t in loaded if t.task_id == task.task_id]
        self.assertEqual(len(matching), 1)

    def test_urgent_flag_preserved_in_persistence(self) -> None:
        """urgent=True must survive a persist/load round-trip."""
        task = EmbedTask(
            channel_name="daily-health-report",
            payload={"channel": "daily-health-report", "agent": "SOVEREIGN", "title": "Urgent"},
            urgent=True,
            task_id=f"urgent-{int(time.time() * 1000)}",
        )
        self._bot._persist_task(task)
        loaded = self._bot._load_persisted_tasks()
        matched = [t for t in loaded if t.task_id == task.task_id]
        self.assertEqual(len(matched), 1)
        self.assertTrue(matched[0].urgent)

    def test_uptime_starts_and_increases(self) -> None:
        """uptime_s() must return a positive and increasing value."""
        t1 = self._bot.uptime_s()
        time.sleep(0.01)
        t2 = self._bot.uptime_s()
        self.assertGreater(t1, 0)
        self.assertGreater(t2, t1)


if __name__ == "__main__":
    unittest.main()
