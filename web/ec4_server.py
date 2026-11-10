#!/usr/bin/env python3
"""EC4 Live — local web server for the IMILAB EC4 cameras.

Open http://127.0.0.1:8765/ on the Mac.  The direct path opens the camera with
the private relay viewer, receives RTP on the Mac, and streams browser-friendly
H.264 fMP4.  A Settings page provides a normal Imilab login form; credentials
live in the macOS Keychain, never in a cookie.

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
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ec4_adb import AdbController, AdbError
from ec4_capture import Capture, CaptureError, MSE_CODEC
from ec4_direct import DirectCapture
from ec4_mac import MacCapture
import imi_login
import ec4_config
import ec4_scan

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(WEB_DIR, "static")
CAMERA_CACHE = os.path.join(WEB_DIR, "cameras.json")
DEFAULT_PORT = int(os.environ.get("EC4_PORT", "8765"))

# Camera display names configured in ~/.ec4/config.json.  The private values
# (iotIds) live there, not in the repository.
KNOWN_CAMERAS = list(ec4_config.camera_ids())


class AppState:
    def __init__(self) -> None:
        self.logs: collections.deque[str] = collections.deque(maxlen=300)
        self.select_lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.mac_mode = os.environ.get("EC4_MAC", "1") == "1"
        self.direct_mode = os.environ.get("EC4_DIRECT", "1") == "1"
        try:
            self.adb: Optional[AdbController] = AdbController(log=self.log)
            self.adb_error = ""
        except Exception as exc:
            self.adb = None
            self.adb_error = str(exc)
        if self.direct_mode:
            self.capture = DirectCapture(log=self.log)
        elif self.mac_mode:
            self.capture = MacCapture(self.adb, log=self.log)
        else:
            self.capture = Capture(log=self.log)
        self.started_at = time.time()
        self.session_token = ""
        self.camera_names, self.camera_layout = self._load_camera_cache()
        self.adb_cameras: list[dict[str, Any]] = []

    def _load_camera_cache(self) -> tuple[list[str], dict[str, dict[str, Any]]]:
        try:
            with open(CAMERA_CACHE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            names = data.get("cameras", [])
            layout = data.get("layout", {})
            if isinstance(names, list) and names and all(isinstance(n, str) for n in names):
                return names, layout if isinstance(layout, dict) else {}
        except Exception:
            pass
        return list(KNOWN_CAMERAS), {}

    def _save_camera_cache(self, names: list[str], layout: dict[str, dict[str, Any]]) -> None:
        try:
            with open(CAMERA_CACHE, "w", encoding="utf-8") as fh:
                json.dump({"cameras": names, "layout": layout}, fh, indent=2)
        except Exception:
            pass

    def log(self, message: str) -> None:
        line = time.strftime("%H:%M:%S ") + str(message)
        self.logs.append(line)
        print(line, flush=True)

    # -- cameras ------------------------------------------------------------
    def cameras(self, force: bool = False) -> dict[str, Any]:
        if self.adb is None and not self.direct_mode:
            return {"ok": False, "error": self.adb_error or "adb not available", "cameras": []}
        if self.direct_mode:
            # The direct Mac path has no Android device to scan; the camera
            # names are fixed in KNOWN_CAMERAS / cameras.json.
            force = False

        warning = ""
        live = self.capture.encoder is not None and not self.capture.encoder.closed
        if force and live:
            warning = "Stop the live view before rescanning the camera list."
            force = False

        if force:
            with self.scan_lock:
                try:
                    scanned = self.adb.cameras(force=True)
                except Exception as exc:
                    return {"ok": False, "error": str(exc), "cameras": []}
            if scanned:
                names = [cam["name"] for cam in scanned]
                for known in KNOWN_CAMERAS:
                    if known not in names:
                        names.append(known)
                self.camera_names = names
                self.adb_cameras = scanned
                for missing in KNOWN_CAMERAS:
                    if not any(cam["name"] == missing for cam in self.adb_cameras):
                        self.adb_cameras.append(
                            {
                                "name": missing,
                                "playing": False,
                                "visible": True,
                                "bounds": None,
                                "play": None,
                                "pause": None,
                            }
                        )
                layout: dict[str, dict[str, Any]] = {}
                for cam in self.adb_cameras:
                    if cam.get("page") is not None and cam.get("play"):
                        layout[cam["name"]] = {"page": cam["page"], "play": cam["play"]}
                self.camera_layout = layout
                self._save_camera_cache(self.camera_names, layout)
                self.log(
                    "camera list: "
                    + ", ".join(self.camera_names)
                    + (" | fast layout: " + ", ".join(sorted(layout)) if layout else "")
                )
            else:
                warning = "Could not read the camera list; using the cached list."

        if not self.adb_cameras:
            names = self.camera_names or KNOWN_CAMERAS
            self.adb_cameras = [
                {
                    "name": name,
                    "playing": False,
                    "visible": True,
                    "bounds": None,
                    "play": None,
                    "pause": None,
                }
                for name in names
            ]

        playing = {cam["name"] for cam in self.adb_cameras if cam.get("playing")}
        if self.capture.selected_camera:
            playing.add(self.capture.selected_camera)
        out = [
            {
                "name": cam["name"],
                "playing": cam["name"] in playing,
                "visible": bool(cam.get("visible", True)),
                "active": cam["name"] == self.capture.selected_camera,
                "linkvisual": cam["name"] in KNOWN_CAMERAS,
            }
            for cam in self.adb_cameras
        ]
        result = {"ok": True, "cameras": out, "active": self.capture.selected_camera}
        if warning:
            result["warning"] = warning
        return result

    # -- select -------------------------------------------------------------
    def select(self, name: str, timeout: float = 45.0) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise CaptureError("camera name is required")
        if isinstance(self.capture, DirectCapture):
            with self.select_lock:
                self.log(f"selecting camera {name} (direct Mac path)")
                st = self.capture.select(name, timeout=timeout)
                self.log(
                    f"{name} live (direct): {st.get('width')}x{st.get('height')} "
                    f"rtp={((st.get('session') or {}).get('rtp'))} "
                    f"keyframes={((st.get('session') or {}).get('keyframes'))}"
                )
                return self._result(name, st)
        if self.adb is None:
            raise CaptureError(self.adb_error or "adb not available")

        with self.select_lock:
            if isinstance(self.capture, MacCapture):
                self.log(f"selecting camera {name} (Mac media path)")
                st = self.capture.select(name, timeout=timeout)
                self.log(
                    f"{name} live (Mac): {st.get('width')}x{st.get('height')} "
                    f"rtp={((st.get('session') or {}).get('rtp'))} "
                    f"keyframes={((st.get('session') or {}).get('keyframes'))}"
                )
                return self._result(name, st)

            current = self.capture.encoder
            if (
                self.capture.selected_camera == name
                and current is not None
                and not current.closed
                and current.wait_ready(1.0)
            ):
                st = current.stats()
                return self._result(name, st)

            # Make sure the app and the Frida session are up.
            self.adb.ensure_home()
            if not self.capture.connected:
                self.capture.connect()
            if not self.capture.connected:
                deadline = time.time() + 20.0
                while time.time() < deadline and not self.capture.connected:
                    time.sleep(0.5)
            if not self.capture.connected:
                raise CaptureError(
                    "Frida is not attached to Imilab Home. Is frida-server running? "
                    "Run tools/start_emulator.sh, then try again."
                )

            prev = self.capture.active_instance()
            prev_inst = prev.inst if prev else None
            t_before = time.time()
            self.log(f"selecting camera {name}")
            fast_used = False
            layout = self.camera_layout.get(name)
            if layout and self.capture.selected_camera and self.capture.selected_camera != name:
                try:
                    info = self.adb.fast_play(name, layout)
                    fast_used = True
                except Exception as exc:
                    self.log(f"fast switch missed ({exc}); using full camera search")
                    info = self.adb.play(name)
            else:
                info = self.adb.play(name)
            already = bool(info.get("already"))
            self.capture.set_camera(name)
            t0 = time.time()

            rec = None
            if already and prev is not None:
                # The target was already the playing tile; reuse its decoder.
                time.sleep(0.4)
                rec = self.capture.active_instance(since=0.0, min_frames=1)
            else:
                if fast_used:
                    rec = self._wait_for_new(t0, prev_inst, min(18.0, timeout))
                    if rec is None:
                        self.log("fast switch did not start a decoder; retrying full camera search")
                        self.adb.play(name)
                        t0 = time.time()
                if rec is None:
                    rec = self._wait_for_new(t0, prev_inst, timeout)

            if rec is None:
                raise CaptureError(
                    f"{name} is playing in the app but no Link Visual decoder output "
                    "was captured. The camera may not be a local Link Visual camera."
                )

            if not self.capture.select(rec.inst):
                raise CaptureError(f"could not start the encoder for {name}")

            encoder = self.capture.encoder
            if encoder is not None:
                encoder.wait_ready(12.0)
            stats = encoder.stats() if encoder is not None else {}
            self.log(
                f"{name} live: {stats.get('width', rec.width)}x{stats.get('height', rec.height)} "
                f"decoder {rec.short}"
            )
            return self._result(name, stats)

    def _wait_for_new(self, t0: float, prev_inst: Optional[str], timeout: float) -> Optional[Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            cand = self.capture.active_instance(since=t0)
            if cand is not None and cand.inst != prev_inst:
                return cand
            # If the vendor app reused the same decoder object, accept it once
            # enough fresh frames have arrived.
            if cand is not None and cand.inst == prev_inst and cand.frames_since(t0) >= 8:
                return cand
            time.sleep(0.2)
        return None

    @staticmethod
    def _result(name: str, stats: dict[str, Any]) -> dict[str, Any]:
        inst = stats.get("inst", "")
        return {
            "ok": True,
            "camera": name,
            "inst": inst,
            "short": stats.get("short", ""),
            "width": stats.get("width", 0),
            "height": stats.get("height", 0),
            "codec": MSE_CODEC,
            "stream": "/stream/" + urllib.parse.quote(str(inst), safe=""),
        }

    def stop(self) -> dict[str, Any]:
        with self.select_lock:
            if isinstance(self.capture, (MacCapture, DirectCapture)):
                self.capture.stop()
            else:
                self.capture.stop_encoder()
            try:
                if self.adb is not None and not self.direct_mode:
                    self.adb.pause_all()
            except Exception:
                pass
            return {"ok": True}

    def status(self) -> dict[str, Any]:
        st = self.capture.status()
        st["ok"] = True
        st["adb"] = self.adb is not None
        st["adb_error"] = self.adb_error
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
        if path == "/api/scan":
            query = urllib.parse.parse_qs(parsed.query)
            cidr = query.get("cidr", [None])[0]
            try:
                self._json(ec4_scan.scan(cidr))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/health":
            self._json({
                "ok": True,
                "adb": STATE.adb is not None,
                "capture": bool(
                    getattr(STATE.capture, "connected", False)
                    or getattr(STATE.capture, "mode", "") in ("mac", "direct")
                ),
                "mode": getattr(STATE.capture, "mode", "frida"),
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
            try:
                result = STATE.select(name)
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
        self.send_header("X-EC4-Codec", MSE_CODEC)
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
            target=lambda: (time.sleep(0.6), subprocess.run(["open", url])),
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
