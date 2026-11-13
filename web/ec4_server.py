#!/usr/bin/env python3
"""EC4 Live — local web server for the IMILAB EC4 cameras.

Open http://127.0.0.1:8765/ on the Mac.  The direct path opens the camera with
the private relay viewer, receives RTP on the Mac, and streams browser-friendly
H.264 fMP4 or an H.265 passthrough.  A Settings page provides a normal Imilab
login form; credentials live in the operating system credential store, never
in a cookie.

Run with ../run_web.sh (or double-click ../EC4 Web.command).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import secrets
import socket
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ec4_direct import DirectCapture
from ec4_encoder import CaptureError, MSE_CODEC
import imi_login
import imi_auth
import ec4_config
import ec4_scan

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(WEB_DIR, "static")
DEFAULT_PORT = int(os.environ.get("EC4_PORT", "8765"))


class AppState:
    def __init__(self) -> None:
        self.logs: collections.deque[str] = collections.deque(maxlen=300)
        self.select_lock = threading.Lock()
        self.capture = DirectCapture(log=self.log)
        self.started_at = time.time()
        self.session_token = ""
        self.camera_names = list(ec4_config.camera_ids())

    def log(self, message: str) -> None:
        line = time.strftime("%H:%M:%S ") + str(message)
        self.logs.append(line)
        print(line, flush=True)

    # -- cameras ------------------------------------------------------------
    def cameras(self, force: bool = False) -> dict[str, Any]:
        names = list(ec4_config.camera_ids()) or self.camera_names
        self.camera_names = names
        active = self.capture.selected_camera
        hosts = ec4_config.host_map()
        networks = ec4_config.local_networks()
        cameras = []
        for name in names:
            host = hosts.get(name) or {}
            cameras.append({
                "name": name,
                "playing": name == active,
                "visible": True,
                "active": name == active,
                "linkvisual": True,
                "lan_ip": str(host.get("ip") or ""),
                "lan_mac": str(host.get("mac") or ""),
                "lan_bound_at": str(host.get("last_seen") or ""),
                "lan_state": ec4_config.lan_state(name, networks),
            })
        return {"ok": True, "cameras": cameras, "active": active}

    # -- select -------------------------------------------------------------
    def select(
        self, name: str, timeout: float = 90.0, codec: str = "h264"
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise CaptureError("camera name is required")
        with self.select_lock:
            self.log(f"selecting camera {name} (direct Mac path, {codec})")
            st = self.capture.select(name, timeout=timeout, codec=codec)
            self.log(
                f"{name} live (direct): {st.get('width')}x{st.get('height')} "
                f"codec={st.get('codec')} "
                f"rtp={((st.get('session') or {}).get('rtp'))} "
                f"keyframes={((st.get('session') or {}).get('keyframes'))}"
            )
            return self._result(name, st)

    def remove_camera(self, name: str) -> dict[str, Any]:
        """Stop a live camera and delete it from the local config."""
        name = (name or "").strip()
        if not name:
            raise CaptureError("camera name is required")
        with self.select_lock:
            if name not in ec4_config.camera_ids():
                raise CaptureError("camera is not configured")
            active = self.capture.selected_camera == name
            if active:
                self.log(f"removing active camera {name}; stopping capture")
                self.capture.stop()
            removed = ec4_config.remove_camera(name)
            remaining = ec4_config.camera_ids()
            self.camera_names = list(remaining)
            return {
                "ok": True,
                "camera": removed,
                "stopped": active,
                "cameras": [
                    {"name": n, "iot_id": i}
                    for n, i in remaining.items()
                ],
            }

    @staticmethod
    def _result(name: str, stats: dict[str, Any]) -> dict[str, Any]:
        inst = stats.get("inst", "direct")
        return {
            "ok": True,
            "camera": name,
            "inst": inst,
            "short": stats.get("short", "direct"),
            "width": stats.get("width", 0),
            "height": stats.get("height", 0),
            "codec": stats.get("codec_string") or MSE_CODEC,
            "codec_name": stats.get("codec", "h264"),
            "passthrough": bool(stats.get("passthrough", False)),
            "stream": "/stream/" + urllib.parse.quote(str(inst), safe=""),
        }

    def stop(self) -> dict[str, Any]:
        with self.select_lock:
            self.capture.stop()
            return {"ok": True}

    def status(self) -> dict[str, Any]:
        st = self.capture.status()
        st["ok"] = True
        st["logs"] = list(self.logs)[-40:]
        return st


STATE: Optional[AppState] = None


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- helpers ------------------------------------------------------------
    def _json(self, payload: dict[str, Any], code: int = 200,
              headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the console quiet for static requests.
        if self.path.startswith("/api/") or self.path.startswith("/stream/"):
            return
        super().log_message(fmt, *args)

    # -- static -------------------------------------------------------------
    def _serve_file(self, path: str) -> None:
        full = os.path.realpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1].lower()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        assert STATE is not None
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_file("index.html")
            return
        if path == "/settings":
            self._serve_file("settings.html")
            return
        if path == "/api/auth/status":
            st = imi_login.status()
            st["ok"] = True
            st["session"] = bool(
                STATE.session_token
                and self._cookie("ec4_session") == STATE.session_token
            )
            self._json(st)
            return
        if path.startswith("/static/"):
            self._serve_file(path[len("/static/") :])
            return
        if path == "/api/cameras":
            query = urllib.parse.parse_qs(parsed.query)
            force = query.get("force", ["0"])[0] in ("1", "true", "yes")
            self._json(STATE.cameras(force=force))
            return
        if path == "/api/status":
            self._json(STATE.status())
            return
        if path == "/api/scan/interfaces":
            self._json({"ok": True, "interfaces": ec4_config.local_networks()})
            return
        if path == "/api/scan":
            query = urllib.parse.parse_qs(parsed.query)
            cidr = query.get("cidr", [None])[0]
            try:
                self._json(ec4_scan.scan(cidr))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/cloud/cameras":
            try:
                imi_login.ensure_iot_token()
                cameras = imi_auth.cloud_cameras()
                configured = ec4_config.camera_ids()
                configured_by_id = {iot_id: name for name, iot_id in configured.items()}
                hosts = ec4_config.host_map()
                for camera in cameras:
                    local_name = configured_by_id.get(camera["iot_id"])
                    host = hosts.get(local_name or "") or {}
                    camera["configured"] = local_name is not None
                    camera["configured_name"] = local_name or ""
                    camera["lan_ip"] = str(host.get("ip") or "")
                    camera["lan_bound_at"] = str(host.get("last_seen") or "")
                self._json({"ok": True, "cameras": cameras})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/health":
            self._json({
                "ok": True,
                "capture": bool(
                    getattr(STATE.capture, "connected", False)
                    or getattr(STATE.capture, "mode", "") == "direct"
                ),
                "mode": "direct",
            })
            return
        if path.startswith("/stream/"):
            inst = urllib.parse.unquote(path[len("/stream/") :])
            self._stream(inst)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(404)

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        assert STATE is not None
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/scan/remember":
            body = self._read_json()
            try:
                result = ec4_scan.remember(
                    str(body.get("name", "")),
                    str(body.get("ip", "")),
                    str(body.get("mac", "")),
                )
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
                return
            self._json(result)
            return
        if path == "/api/cameras/add":
            body = self._read_json()
            try:
                added = ec4_config.add_camera(
                    str(body.get("name", "")),
                    str(body.get("iot_id", "")),
                    str(body.get("ip", "")),
                    str(body.get("mac", "")),
                )
                STATE.camera_names = list(ec4_config.camera_ids())
                self._json({
                    "ok": True,
                    "camera": added,
                    "cameras": [
                        {"name": name, "iot_id": iot_id}
                        for name, iot_id in ec4_config.camera_ids().items()
                    ],
                })
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/cameras/remove":
            body = self._read_json()
            try:
                self._json(STATE.remove_camera(str(body.get("name", ""))))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/auth/login":
            body = self._read_json()
            try:
                result = imi_login.login(
                    str(body.get("username", "")),
                    str(body.get("password", "")),
                )
            except Exception as exc:
                STATE.log(f"login failed: {exc}")
                self._json({"ok": False, "error": str(exc)}, 401)
                return
            if result.get("needs_verification"):
                self._json(result)
                return
            STATE.session_token = secrets.token_urlsafe(32)
            STATE.log(f"login ok: {result.get('username')} "
                      f"(iot={'ok' if result.get('iot_ok') else 'needs-bootstrap'})")
            self._json(
                result,
                headers={
                    "Set-Cookie": (
                        "ec4_session=" + STATE.session_token
                        + "; HttpOnly; SameSite=Strict; Path=/"
                    )
                },
            )
            return
        if path == "/api/auth/logout":
            imi_login.logout()
            STATE.session_token = ""
            self._json(
                {"ok": True},
                headers={
                    "Set-Cookie": "ec4_session=; Max-Age=0; HttpOnly; "
                                  "SameSite=Strict; Path=/"
                },
            )
            return
        if path == "/api/select":
            body = self._read_json()
            name = str(body.get("name", "")).strip()
            codec = str(body.get("codec", "h264")).strip().lower()
            if codec not in ("h264", "h265"):
                self._json({"ok": False, "error": "unsupported codec"}, 400)
                return
            try:
                result = STATE.select(name, codec=codec)
                self._json(result)
            except Exception as exc:
                STATE.log(f"select failed: {exc}")
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/stop":
            self._json(STATE.stop())
            return
        if path == "/api/refresh":
            self._json(STATE.cameras(force=True))
            return
        self.send_error(404)

    # -- stream -------------------------------------------------------------
    def _write_chunk(self, payload: bytes) -> None:
        self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream(self, inst: str) -> None:
        assert STATE is not None
        cap = STATE.capture
        with cap.lock:
            encoder = cap.encoder
            selected = cap.selected_inst
        if encoder is None or selected != inst:
            self._json({"ok": False, "error": "stream is not active"}, 404)
            return
        sub = encoder.subscribe(timeout=12.0)
        if sub is None:
            self._json({"ok": False, "error": "encoder is still starting"}, 503)
            return
        init, fragments, seq = sub
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "X-EC4-Codec",
            encoder.stats().get("codec_string") or MSE_CODEC,
        )
        self.end_headers()
        STATE.log(f"browser stream connected ({encoder.stats()['short']})")
        misses = 0
        try:
            self._write_chunk(struct.pack(">I", len(init)) + init)
            for frag in fragments:
                self._write_chunk(struct.pack(">I", len(frag)) + frag)
            while True:
                with cap.lock:
                    if cap.encoder is not encoder:
                        break
                if encoder.closed:
                    break
                result = encoder.wait_fragments(seq, timeout=12.0)
                if result is None:
                    break
                new_frags, seq = result
                if not new_frags:
                    misses += 1
                    if misses >= 2:
                        break
                    continue
                misses = 0
                for frag in new_frags:
                    self._write_chunk(struct.pack(">I", len(frag)) + frag)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            encoder.release_client()
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
            STATE.log("browser stream disconnected")


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> int:
    global STATE
    parser = argparse.ArgumentParser(description="EC4 local web viewer")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default localhost)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    STATE = AppState()
    STATE.log("EC4 web server starting")
    server = QuietServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    STATE.log(f"listening on {url}")
    if args.host == "0.0.0.0":
        STATE.log(f"LAN URL: http://{lan_ip()}:{args.port}/")

    if not args.no_open:
        threading.Thread(
            target=lambda: (time.sleep(0.6), webbrowser.open(url)),
            daemon=True,
        ).start()

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        STATE.log("shutting down")
    finally:
        STATE.capture.close()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
