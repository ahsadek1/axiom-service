"""
status_api.py — Data Source Status HTTP API
Port 8097
GET /data-sources        → full status of all sources
GET /data-sources/active → currently active source per category
GET /data-sources/events → recent transition events
GET /data-sources/health → simple health summary
"""
from __future__ import annotations
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import json as _json

log = logging.getLogger("nexus.data_sources.api")


def start_status_api(port: int = 8097) -> None:
    from .health_monitor import get_all_status, get_recent_events
    from .fallback_manager import get_active_summary

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path == "/data-sources":
                    body = _json.dumps(get_all_status(), indent=2).encode()
                elif self.path == "/data-sources/active":
                    body = _json.dumps(get_active_summary(), indent=2).encode()
                elif self.path == "/data-sources/events":
                    body = _json.dumps(get_recent_events(24), indent=2).encode()
                elif self.path == "/data-sources/health":
                    status = get_all_status()
                    body = _json.dumps({
                        "status":  "healthy" if status.get("failed",0) == 0 else "degraded",
                        "total":   status.get("total", 0),
                        "healthy": status.get("healthy", 0),
                        "failed":  status.get("failed", 0),
                        "degraded":status.get("degraded", 0),
                    }, indent=2).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(exc).encode())

        def log_message(self, *a): pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), H).serve_forever(),
        daemon=True, name="data-source-api",
    ).start()
    log.info("Data source status API on port %d", port)
