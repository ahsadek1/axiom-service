#!/usr/bin/env python3
"""
Combined Position Gate — SHARED across Alpha and Prime

Ground truth: Alpaca live positions via /v2/positions
This is the ONLY source of truth for position count across all services.

Usage:
    from shared.combined_position_gate import get_live_position_count_safe
    
    try:
        count = get_live_position_count_safe(
            alpaca_url="https://paper-api.alpaca.markets",
            api_key="...",
            secret_key="..."
        )
        if count >= MAX_CONCURRENT_POSITIONS:
            block_execution("Position cap reached")
    except PositionCountError:
        # Alpaca unreachable — fail closed (do not execute)
        block_execution("Position count unavailable")

CRITICAL RULE:
  - Both Alpha and Prime MUST call get_live_position_count_safe() before ANY order
  - Comparing local DB counts is NOT sufficient — they can desync
  - Alpaca /v2/positions is the canonical source of truth
  - On error: FAIL CLOSED (do not execute if we cannot verify)
"""

import requests
from typing import Optional


class PositionCountError(Exception):
    """Raised when position count cannot be determined"""
    pass


def get_live_position_count_safe(
    alpaca_url: str,
    api_key: str,
    secret_key: str,
    timeout_sec: int = 5,
) -> int:
    """
    Fetch live position count from Alpaca /v2/positions.
    
    Args:
        alpaca_url: Base URL for Alpaca (e.g., https://paper-api.alpaca.markets)
        api_key: APCA-API-KEY-ID
        secret_key: APCA-API-SECRET-KEY
        timeout_sec: Request timeout
    
    Returns:
        Integer count of open positions
    
    Raises:
        PositionCountError: If Alpaca is unreachable or returns invalid response
    """
    try:
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        
        resp = requests.get(
            f"{alpaca_url}/v2/positions",
            headers=headers,
            timeout=timeout_sec
        )
        
        if resp.status_code != 200:
            raise PositionCountError(
                f"Alpaca /v2/positions returned HTTP {resp.status_code}: {resp.text}"
            )
        
        positions = resp.json()
        
        if not isinstance(positions, list):
            raise PositionCountError(
                f"Alpaca /v2/positions returned non-list: {type(positions)}"
            )
        
        # Count only positions with non-zero qty
        count = sum(1 for p in positions if float(p.get("qty", 0)) != 0)
        
        return count
        
    except requests.RequestException as e:
        raise PositionCountError(f"Alpaca unreachable: {e}")
    except (ValueError, KeyError) as e:
        raise PositionCountError(f"Invalid response from Alpaca: {e}")
    except Exception as e:
        raise PositionCountError(f"Unexpected error fetching position count: {e}")


def check_position_limit(
    alpaca_url: str,
    api_key: str,
    secret_key: str,
    max_positions: int = 3,
    timeout_sec: int = 5,
) -> tuple[bool, int, str]:
    """
    Check if we can accept a new position.
    
    Returns:
        (allowed: bool, current_count: int, reason: str)
    """
    try:
        count = get_live_position_count_safe(alpaca_url, api_key, secret_key, timeout_sec)
        
        if count >= max_positions:
            return (
                False,
                count,
                f"Position cap: {count}/{max_positions} positions already open"
            )
        
        return (True, count, f"Position cap OK: {count}/{max_positions}")
        
    except PositionCountError as e:
        # FAIL CLOSED: Do not allow execution if we cannot verify
        return (
            False,
            -1,  # Unknown
            f"Position count verification failed: {str(e)}"
        )
