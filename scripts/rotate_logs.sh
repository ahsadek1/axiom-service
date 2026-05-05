#!/bin/bash
# rotate_logs.sh — Nexus log rotation
# Keeps logs under 50MB. Rotates when over threshold. Keeps 3 rotations.
# Run daily via launchd.

LOG_DIR="$HOME/nexus/logs"
MAX_SIZE_MB=50
KEEP_ROTATIONS=3

rotate_if_large() {
  local log_file="$1"
  if [ ! -f "$log_file" ]; then return; fi
  local size_mb=$(du -m "$log_file" | cut -f1)
  if [ "$size_mb" -gt "$MAX_SIZE_MB" ]; then
    echo "$(date): Rotating $log_file (${size_mb}MB)"
    # Shift existing rotations
    for i in $(seq $((KEEP_ROTATIONS-1)) -1 1); do
      [ -f "${log_file}.$i" ] && mv "${log_file}.$i" "${log_file}.$((i+1))"
    done
    # Rotate current
    cp "$log_file" "${log_file}.1"
    truncate -s 0 "$log_file"
    # Remove old rotations beyond keep count
    for i in $(seq $((KEEP_ROTATIONS+1)) 9); do
      rm -f "${log_file}.$i"
    done
  fi
}

# Rotate all service logs
find "$LOG_DIR" -name "*.log" | while read log; do
  rotate_if_large "$log"
done

echo "$(date): Log rotation complete"
