"""Lifecycle management for the loopback HTTP server."""

from __future__ import annotations

import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CLIENT_HEARTBEAT_TIMEOUT_SECONDS = 8.0
CLIENT_CLOSE_GRACE_SECONDS = 3.0
CLIENT_STARTUP_TIMEOUT_SECONDS = 30.0


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client_lifecycle_enabled = False
        self._client_lifecycle_started = time.monotonic()
        self._client_last_seen: float | None = None
        self._client_closed_at: float | None = None
        self._client_lifecycle_lock = threading.Lock()
        self._client_shutdown_started = False
        self._client_monitor_thread: threading.Thread | None = None

    def enable_client_lifecycle(self) -> None:
        with self._client_lifecycle_lock:
            if self._client_lifecycle_enabled:
                return
            self._client_lifecycle_enabled = True
            self._client_lifecycle_started = time.monotonic()
            self._client_monitor_thread = threading.Thread(
                target=self._monitor_client,
                name="webui-client-monitor",
                daemon=True,
            )
            self._client_monitor_thread.start()

    def client_heartbeat(self) -> None:
        with self._client_lifecycle_lock:
            self._client_last_seen = time.monotonic()
            self._client_closed_at = None

    def client_closed(self) -> None:
        with self._client_lifecycle_lock:
            if not self._client_lifecycle_enabled or self._client_shutdown_started:
                return
            self._client_closed_at = time.monotonic()

    def _monitor_client(self) -> None:
        while True:
            time.sleep(0.5)
            with self._client_lifecycle_lock:
                if self._client_shutdown_started:
                    return
                now = time.monotonic()
                last_seen = self._client_last_seen
                closed_at = self._client_closed_at
                startup_expired = (
                    last_seen is None
                    and now - self._client_lifecycle_started >= CLIENT_STARTUP_TIMEOUT_SECONDS
                )
                close_beacon_expired = (
                    closed_at is not None
                    and now - closed_at >= CLIENT_CLOSE_GRACE_SECONDS
                    and (last_seen is None or last_seen < closed_at)
                )
                heartbeat_expired = (
                    last_seen is not None
                    and now - last_seen >= CLIENT_HEARTBEAT_TIMEOUT_SECONDS
                )
                if not (startup_expired or close_beacon_expired or heartbeat_expired):
                    continue
                self._client_shutdown_started = True
            self.shutdown()
            return


def create_server(handler: type[BaseHTTPRequestHandler], port: int = 0,
                  open_browser: bool = True) -> tuple[Server, str]:
    server = Server(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    if open_browser:
        server.enable_client_lifecycle()
        webbrowser.open(url)
    return server, url
