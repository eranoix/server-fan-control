"""Dashboard and JSON API on the standard library's HTTP server.

Routes
    GET  /                         dashboard (static files from ./web)
    GET  /healthz                  liveness, no auth
    GET  /api/status               current readings, fan modes, recent events
    GET  /api/history?since=<t>    samples newer than unix time t
    GET  /api/config               sensors, fans and presets
    PUT  /api/fans/<id>            change curve, source, min_pwm or hysteresis_c
    POST /api/fans/<id>/preset     {"preset": "quiet"}
    POST /api/demo/workload        demo only: {"workload": "stress"}
    POST /api/demo/fault           demo only: {"sensor": "nvme", "fault": "missing"}
    POST /api/demo/crash           demo only: make the next control tick raise

When a token file is configured every /api route requires
``Authorization: Bearer <token>``. The default bind is loopback.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import ConfigError, config_to_dict, save_config, update_fan
from .controller import Controller
from .curve import PRESETS

log = logging.getLogger("fancurve.http")

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 64 * 1024
STATIC = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/favicon.svg": "favicon.svg",
}


@dataclass
class DemoHooks:
    """Present only when running the simulator in-process."""

    set_workload: Callable[[str], None]
    set_fault: Callable[[str, str | None], None]
    summary: Callable[[], dict]


class App:
    def __init__(
        self,
        controller: Controller,
        config_path: str | Path | None = None,
        token: str | None = None,
        demo: DemoHooks | None = None,
    ) -> None:
        self.controller = controller
        self.config_path = Path(config_path) if config_path else None
        self.token = token or None
        self.demo = demo
        self.write_lock = threading.Lock()

    def change_fan(self, fan_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self.write_lock:
            new = update_fan(self.controller.config, fan_id, changes)
            if self.config_path is not None:
                save_config(new, self.config_path)
            self.controller.set_config(new)
            self.controller.event("info", f"fan {fan_id}: updated {', '.join(sorted(changes))}")
        self.controller.tick()
        return config_to_dict(new)["fans"][fan_id]

    def config_view(self) -> dict[str, Any]:
        d = config_to_dict(self.controller.config)
        return {
            "chip": d["chip"],
            "interval_s": d["interval_s"],
            "sensors": d["sensors"],
            "fans": d["fans"],
            "failsafe": d["failsafe"],
            "presets": {k: [list(p) for p in v] for k, v in PRESETS.items()},
            "demo": self.demo is not None,
        }


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "fancurve"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str, cache: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; frame-ancestors 'none'",
            )
            self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _error(self, status: int, message: str, errors: list[str] | None = None) -> None:
            body = {"error": message}
            if errors:
                body["errors"] = errors
            self._json(status, body)

        def _authorized(self) -> bool:
            if not app.token:
                return True
            got = self.headers.get("Authorization", "")
            if not got.startswith("Bearer "):
                return False
            return hmac.compare_digest(got[7:].strip().encode(), app.token.encode())

        def _body(self) -> Any:
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                raise ValueError("Content-Length required")
            n = int(length)
            if n > MAX_BODY:
                raise ValueError("body too large")
            ctype = self.headers.get("Content-Type", "")
            if not ctype.startswith("application/json"):
                raise ValueError("Content-Type must be application/json")
            try:
                return json.loads(self.rfile.read(n) or b"null")
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON: {exc.msg}") from exc

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            path = url.path
            if path in STATIC:
                return self._static(STATIC[path])
            if path == "/healthz":
                return self._json(200, {"ok": True, "state": app.controller.overall_state()})
            if not path.startswith("/api/"):
                return self._error(404, "not found")
            if not self._authorized():
                return self._error(401, "missing or wrong bearer token")
            if path == "/api/status":
                snap = app.controller.snapshot()
                snap["demo"] = app.demo.summary() if app.demo else None
                return self._json(200, snap)
            if path == "/api/history":
                qs = parse_qs(url.query)
                try:
                    since = float(qs.get("since", ["0"])[0])
                except ValueError:
                    return self._error(400, "since must be a number")
                with app.controller.lock:
                    samples = [s for s in app.controller.history if s["t"] > since]
                return self._json(200, {"samples": samples})
            if path == "/api/config":
                return self._json(200, app.config_view())
            return self._error(404, "not found")

        def do_PUT(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if not self._authorized():
                return self._error(401, "missing or wrong bearer token")
            parts = path.strip("/").split("/")
            if len(parts) != 3 or parts[:2] != ["api", "fans"]:
                return self._error(404, "not found")
            try:
                body = self._body()
                fan = app.change_fan(parts[2], body)
            except ConfigError as exc:
                return self._error(400, "config rejected", exc.errors)
            except ValueError as exc:
                return self._error(400, str(exc))
            return self._json(200, {"ok": True, "fan": fan})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if not self._authorized():
                return self._error(401, "missing or wrong bearer token")
            parts = path.strip("/").split("/")
            try:
                if len(parts) == 4 and parts[:2] == ["api", "fans"] and parts[3] == "preset":
                    body = self._body()
                    name = body.get("preset") if isinstance(body, dict) else None
                    if name not in PRESETS:
                        return self._error(400, f"unknown preset; choose from {', '.join(PRESETS)}")
                    curve = [list(p) for p in PRESETS[name]]
                    fan = app.change_fan(parts[2], {"curve": curve})
                    return self._json(200, {"ok": True, "fan": fan})
                if parts[:2] == ["api", "demo"] and len(parts) == 3:
                    return self._demo(parts[2])
            except ConfigError as exc:
                return self._error(400, "config rejected", exc.errors)
            except (ValueError, KeyError) as exc:
                return self._error(400, str(exc).strip("'\""))
            return self._error(404, "not found")

        def _demo(self, action: str) -> None:
            if app.demo is None:
                return self._error(404, "demo endpoints exist only in demo mode")
            body = self._body()
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if action == "workload":
                app.demo.set_workload(str(body.get("workload", "")))
            elif action == "fault":
                fault = body.get("fault")
                app.demo.set_fault(str(body.get("sensor", "")), fault if fault else None)
            elif action == "crash":
                app.controller.crash_next_tick = "simulated bug in the control loop"
            else:
                return self._error(404, "not found")
            return self._json(200, {"ok": True, "demo": app.demo.summary()})

        def _static(self, name: str) -> None:
            p = WEB_DIR / name
            try:
                body = p.read_bytes()
            except OSError:
                return self._error(404, "not found")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, body, ctype)

    return Handler


def make_server(app: App, bind: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), make_handler(app))
    srv.daemon_threads = True
    return srv
