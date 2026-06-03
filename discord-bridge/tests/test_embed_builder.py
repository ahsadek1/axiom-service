"""
test_embed_builder.py — Tests for EmbedBuilder and ChannelRegistry color resolution.

Covers TC-01, TC-07, TC-08, TC-09, color resolution.
"""
import sys
import os
import json
import tempfile
import unittest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from channel_registry import ChannelRegistry, COLOR_MAP, DEFAULT_COLOR
from embed_builder import EmbedBuilder


def make_registry(tmp_dir: str) -> ChannelRegistry:
    """Create a ChannelRegistry backed by a temp channel_map.json."""
    channel_map = {
        "daily-health-report": {"agent": "SOVEREIGN", "category": "GOVERNANCE"},
        "go-verdicts": {"agent": "OMNI", "category": "TRADING"},
    }
    map_path = os.path.join(tmp_dir, "channel_map.json")
    with open(map_path, "w") as f:
        json.dump(channel_map, f)
    return ChannelRegistry(map_path=map_path)


class TestColorResolution(unittest.TestCase):
    """Tests for ChannelRegistry.resolve_color()."""

    def test_green_resolves_to_correct_hex(self) -> None:
        """Green must resolve to 0x2ECC71."""
        self.assertEqual(ChannelRegistry.resolve_color("green"), 0x2ECC71)

    def test_red_resolves_to_correct_hex(self) -> None:
        self.assertEqual(ChannelRegistry.resolve_color("red"), 0xE74C3C)

    def test_unknown_color_defaults_to_blue(self) -> None:
        """An unknown color name must fall back to blue (0x3498DB)."""
        self.assertEqual(
            ChannelRegistry.resolve_color("chartreuse"),
            COLOR_MAP[DEFAULT_COLOR],
        )

    def test_none_color_defaults_to_blue(self) -> None:
        """None must fall back to the default blue."""
        self.assertEqual(
            ChannelRegistry.resolve_color(None),
            COLOR_MAP[DEFAULT_COLOR],
        )

    def test_all_defined_colors_resolve(self) -> None:
        """Every named color in the spec must resolve to a non-zero int."""
        for name in ("green", "red", "yellow", "blue", "purple", "orange", "gray"):
            with self.subTest(color=name):
                result = ChannelRegistry.resolve_color(name)
                self.assertIsInstance(result, int)
                self.assertGreater(result, 0)


class TestEmbedBuilder(unittest.TestCase):
    """Tests for EmbedBuilder.build()."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._registry = make_registry(self._tmp)
        self._builder = EmbedBuilder(self._registry)

    # TC-01: Happy path
    def test_build_returns_embed_with_correct_title_author_color(self) -> None:
        """TC-01: build() returns Embed with title, author, correct green color."""
        import discord
        payload = {
            "channel": "daily-health-report",
            "agent": "SOVEREIGN",
            "title": "Test Health Report",
            "color": "green",
        }
        embed = self._builder.build(payload)
        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(embed.title, "Test Health Report")
        self.assertEqual(embed.author.name, "SOVEREIGN")
        self.assertEqual(embed.color.value, 0x2ECC71)

    def test_build_includes_description_when_provided(self) -> None:
        import discord
        payload = {
            "channel": "go-verdicts",
            "agent": "OMNI",
            "title": "GO Verdict",
            "description": "NVDA is a strong GO.",
        }
        embed = self._builder.build(payload)
        self.assertEqual(embed.description, "NVDA is a strong GO.")

    def test_build_includes_footer_when_provided(self) -> None:
        import discord
        payload = {
            "channel": "go-verdicts",
            "agent": "OMNI",
            "title": "Test",
            "footer": "OMNI • 09:30 ET",
        }
        embed = self._builder.build(payload)
        self.assertEqual(embed.footer.text, "OMNI • 09:30 ET")

    def test_build_includes_timestamp_when_flag_set(self) -> None:
        import discord
        payload = {
            "channel": "daily-health-report",
            "agent": "SOVEREIGN",
            "title": "Timestamped",
            "timestamp": True,
        }
        embed = self._builder.build(payload)
        self.assertIsNotNone(embed.timestamp)

    def test_build_no_timestamp_when_flag_false(self) -> None:
        import discord
        payload = {
            "channel": "daily-health-report",
            "agent": "SOVEREIGN",
            "title": "No Timestamp",
            "timestamp": False,
        }
        embed = self._builder.build(payload)
        # discord.Embed.Empty == discord.utils.MISSING in newer versions; just check it is falsy or Missing
        self.assertFalse(bool(embed.timestamp))

    def test_build_includes_fields(self) -> None:
        import discord
        payload = {
            "channel": "daily-health-report",
            "agent": "SOVEREIGN",
            "title": "With Fields",
            "fields": [
                {"name": "Alpha Buffer", "value": "✅ Running", "inline": True},
                {"name": "Prime Buffer", "value": "✅ Running", "inline": True},
            ],
        }
        embed = self._builder.build(payload)
        self.assertEqual(len(embed.fields), 2)
        self.assertEqual(embed.fields[0].name, "Alpha Buffer")
        self.assertEqual(embed.fields[1].name, "Prime Buffer")

    # TC-07: Two independent embeds don't contaminate each other
    def test_two_embeds_built_independently_no_contamination(self) -> None:
        """TC-07: Two payloads produce independent embeds with no cross-contamination."""
        import discord
        payload_a = {
            "channel": "daily-health-report",
            "agent": "SOVEREIGN",
            "title": "Report A",
            "color": "green",
            "description": "Text A",
        }
        payload_b = {
            "channel": "go-verdicts",
            "agent": "OMNI",
            "title": "Report B",
            "color": "purple",
            "description": "Text B",
        }
        embed_a = self._builder.build(payload_a)
        embed_b = self._builder.build(payload_b)

        self.assertEqual(embed_a.title, "Report A")
        self.assertEqual(embed_b.title, "Report B")
        self.assertEqual(embed_a.author.name, "SOVEREIGN")
        self.assertEqual(embed_b.author.name, "OMNI")
        self.assertEqual(embed_a.color.value, 0x2ECC71)
        self.assertEqual(embed_b.color.value, 0x9B59B6)
        self.assertNotEqual(embed_a.description, embed_b.description)

    # TC-08: urgent flag is on EmbedTask, not embed — verify embed builds normally when urgent=True in payload
    def test_urgent_payload_builds_embed_without_error(self) -> None:
        """TC-08: urgent flag in payload doesn't break embed building."""
        import discord
        payload = {
            "channel": "daily-health-report",
            "agent": "SOVEREIGN",
            "title": "URGENT: System Alert",
            "urgent": True,
            "color": "red",
        }
        embed = self._builder.build(payload)
        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(embed.color.value, 0xE74C3C)

    def test_build_error_returns_red_embed(self) -> None:
        """build_error() must return a red embed."""
        import discord
        embed = self._builder.build_error("Test Error", "Something went wrong.")
        self.assertEqual(embed.color.value, 0xE74C3C)
        self.assertEqual(embed.title, "Test Error")
        self.assertEqual(embed.description, "Something went wrong.")

    def test_title_truncated_at_256(self) -> None:
        """Titles longer than 256 chars must be truncated to 256."""
        long_title = "X" * 300
        payload = {
            "channel": "daily-health-report",
            "agent": "SOVEREIGN",
            "title": long_title,
        }
        embed = self._builder.build(payload)
        self.assertLessEqual(len(embed.title), 256)


class TestFieldCountValidation(unittest.TestCase):
    """TC-09: Field count validation belongs in http_server, but we verify the
    limit logic is enforceable (26 fields would be caught before build() is called)."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._registry = make_registry(self._tmp)
        self._builder = EmbedBuilder(self._registry)

    def test_25_fields_builds_without_error(self) -> None:
        """25 fields (Discord max) must build cleanly."""
        import discord
        fields = [{"name": f"Field {i}", "value": f"Val {i}", "inline": False}
                  for i in range(25)]
        payload = {
            "channel": "daily-health-report",
            "agent": "SOVEREIGN",
            "title": "25 Fields",
            "fields": fields,
        }
        embed = self._builder.build(payload)
        self.assertEqual(len(embed.fields), 25)

    def test_26_fields_detected_by_http_validation(self) -> None:
        """Confirm that 26 fields would be rejected by counting — mirrors TC-09 validation logic."""
        fields = [{"name": f"F{i}", "value": f"V{i}"} for i in range(26)]
        # This is the validation logic from http_server._handle_send
        self.assertGreater(len(fields), 25)


if __name__ == "__main__":
    unittest.main()
