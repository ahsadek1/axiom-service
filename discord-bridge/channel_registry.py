"""
channel_registry.py — Loads and validates channel_map.json.
Provides channel lookups and rejects unknown channels.
"""
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Color name → Discord integer color
COLOR_MAP: Dict[str, int] = {
    "green":  0x2ECC71,
    "red":    0xE74C3C,
    "yellow": 0xF1C40F,
    "blue":   0x3498DB,
    "purple": 0x9B59B6,
    "orange": 0xE67E22,
    "gray":   0x95A5A6,
    "gold":   0xF39C12,
    "teal":   0x1ABC9C,
    "indigo": 0x8E44AD,
}
DEFAULT_COLOR = "blue"


class ChannelRegistry:
    """Loads channel_map.json and validates channel names."""

    def __init__(self, map_path: str = None) -> None:
        if map_path is None:
            map_path = str(Path(__file__).parent / "config" / "channel_map.json")
        self._path = map_path
        self._map: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        """Load channel map from disk. Raises on missing or malformed file."""
        with open(self._path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"channel_map.json must be a JSON object, got {type(data)}")
        self._map = data
        logger.info(f"ChannelRegistry loaded {len(self._map)} channels: {list(self._map.keys())}")

    def is_known(self, channel_name: str) -> bool:
        """Return True if channel_name is in the map."""
        return channel_name in self._map

    def get(self, channel_name: str) -> Optional[Dict[str, str]]:
        """Return channel metadata dict or None if not found."""
        return self._map.get(channel_name)

    def all_channels(self) -> Dict[str, Dict[str, str]]:
        """Return full channel map."""
        return dict(self._map)

    @staticmethod
    def resolve_color(color_name: Optional[str]) -> int:
        """Convert color name string to Discord integer color. Defaults to blue."""
        return COLOR_MAP.get(color_name or DEFAULT_COLOR, COLOR_MAP[DEFAULT_COLOR])
