"""Frida capture + H.265 -> fragmented MP4 encoder.

The Android app hands plain H.265 NALs to its Link Visual decoder.  This module
taps that decoder, keeps the latest NALs per decoder instance, and (for the
selected camera) pipes them into ffmpeg.  ffmpeg produces a fragmented MP4
stream that the browser plays with Media Source Extensions.

Only native hooks are used, so the Python frida package does not need the Java
bridge (which is a separate bundle in Frida 17).
"""

from __future__ import annotations

import collections
import os
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import frida

APP_NAMES = ("imilab Home", "com.chuangmi.imihome")

FFMPEG = shutil.which("ffmpeg")

# Browser MSE codec string for the H.264 stream we produce:
# Main profile, level 4.0 -> avc1.4D4028.
MSE_CODEC = "avc1.42C028"  # constrained baseline, level 4.0


def short_inst(inst: str) -> str:
    return inst.rsplit("@", 1)[-1][-8:]


def output_size(width: int, height: int) -> tuple[int, int]:
    """The browser stream is capped at 1280x720; keep the source aspect."""
    if width <= 0 or height <= 0:
        return width, height
    if width <= 1280 and height <= 720:
        return width, height
    scale = min(1280.0 / width, 720.0 / height)
    w = max(2, int(width * scale) // 2 * 2)
    h = max(2, int(height * scale) // 2 * 2)
    return w, h


def ffmpeg_cmd() -> list[str]:
    if not FFMPEG:
        raise RuntimeError("ffmpeg not found. Install it with: brew install ffmpeg")
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel", "warning",
        # Do not add -fflags nobuffer here: with wallclock timestamps it
        # drops HEVC reference frames, producing grey/purple macroblocks.
        "-flags", "low_delay",
        "-use_wallclock_as_timestamps", "1",
        "-f", "hevc",
        "-i", "pipe:0",
        "-an",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-profile:v", "main",
        "-level:v", "4.0",
        "-pix_fmt", "yuv420p",
        "-g", "15",
        "-keyint_min", "15",
        "-sc_threshold", "0",
        "-bf", "0",
        "-x264-params", "repeat-headers=1:aud=1",
        "-fps_mode", "vfr",
        "-f", "mp4",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-flush_packets", "1",
        "pipe:1",
    ]


def _read_exact(stream, size: int) -> Optional[bytes]:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


class Encoder:
    """One ffmpeg process producing a fMP4 stream for one decoder instance."""

    def __init__(self, inst: str, extra: bytes, width: int, height: int, log=print) -> None:
        self.inst = inst
        self.extra = extra
        self.source_width = width
        self.source_height = height
        self.width, self.height = output_size(width, height)
        self.log = log

        self.proc = subprocess.Popen(
            ffmpeg_cmd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.init: Optional[bytes] = None
        self.fragments: collections.deque[bytes] = collections.deque(maxlen=240)
        self.frag_seq = 0
        self.closed = False
        self.frames_in = 0
        self.bytes_in = 0
        self.clients = 0
        self.started_at = time.time()
        self.last_fragment_at = 0.0
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=12)
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._err_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._read_thread.start()
        self._err_thread.start()

        # Start at a keyframe.
        self.feed(extra)
        if width and height:
            self.log(f"encoder {short_inst(inst)} {self.width}x{self.height} (source {width}x{height}) started")

    # -- input --------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        if not data or self.closed:
            return
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return
        try:
            view = memoryview(data)
            while view:
                written = proc.stdin.write(view)
                if written is None:
                    raise BrokenPipeError("ffmpeg stdin is not writable")
                if written <= 0:
                    raise BrokenPipeError("ffmpeg stdin accepted no data")
                view = view[written:]
            self.bytes_in += len(data)
            self.frames_in += 1
        except (BrokenPipeError, OSError):
            self._mark_closed()

    # -- output -------------------------------------------------------------
    def _read_box(self) -> Optional[tuple[str, bytes]]:
        stream = self.proc.stdout
        assert stream is not None
        header = _read_exact(stream, 8)
        if header is None:
            return None
        size = int.from_bytes(header[:4], "big")
        typ = header[4:8].decode("latin1", "replace")
        body = b""
        if size == 1:
            ext = _read_exact(stream, 8)
            if ext is None:
                return None
            size = int.from_bytes(ext, "big")
            body = _read_exact(stream, size - 16) or b""
            return typ, header + ext + body
        if size == 0:
            # Box extends to EOF.
            body = stream.read() or b""
            return typ, header + body
        if size < 8:
            return None
        body = _read_exact(stream, size - 8)
        if body is None:
            return None
        return typ, header + body

    def _read_loop(self) -> None:
        init_parts: list[bytes] = []
        current: list[bytes] = []
        try:
            while True:
                box = self._read_box()
                if box is None:
                    break
                typ, data = box
                if self.init is None:
                    init_parts.append(data)
                    if typ == "moov":
                        with self.cond:
                            self.init = b"".join(init_parts)
                            self.cond.notify_all()
                        self.log(f"encoder {short_inst(self.inst)}: MP4 init ready")
                    continue
                if typ == "moof":
                    if current:
                        self._push_fragment(b"".join(current))
                    current = [data]
                elif typ == "mdat":
                    current.append(data)
                    self._push_fragment(b"".join(current))
                    current = []
                elif current:
                    current.append(data)
                # Ignore stray boxes outside a fragment.
        except Exception as exc:
            self.log(f"encoder reader error: {exc}")
        finally:
            if current:
                self._push_fragment(b"".join(current))
            self._mark_closed()

    def _push_fragment(self, fragment: bytes) -> None:
        if not fragment:
            return
        with self.cond:
            self.fragments.append(fragment)
            self.frag_seq += 1
            self.last_fragment_at = time.time()
            self.cond.notify_all()

    def _stderr_loop(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail.append(line)

    def _mark_closed(self) -> None:
        with self.cond:
            if not self.closed:
                self.closed = True
                self.cond.notify_all()

    # -- clients ------------------------------------------------------------
    def wait_ready(self, timeout: float = 12.0) -> bool:
        deadline = time.time() + timeout
        with self.cond:
            while time.time() < deadline:
                if self.init is not None and self.frag_seq > 0:
                    return True
                if self.closed:
                    return False
                self.cond.wait(timeout=0.4)
        return False

    def subscribe(self, timeout: float = 12.0) -> Optional[tuple[bytes, list[bytes], int]]:
        deadline = time.time() + timeout
        with self.cond:
            while time.time() < deadline:
                if self.init is not None and self.frag_seq > 0:
                    self.clients += 1
                    frags = list(self.fragments)[-1:]
                    return self.init, frags, self.frag_seq
                if self.closed:
                    return None
                self.cond.wait(timeout=0.4)
            return None

    def wait_fragments(self, last_seq: int, timeout: float = 15.0) -> Optional[tuple[list[bytes], int]]:
        deadline = time.time() + timeout
        with self.cond:
            while time.time() < deadline:
                if self.frag_seq > last_seq:
                    delta = self.frag_seq - last_seq
                    if delta <= len(self.fragments):
                        frags = list(self.fragments)[-delta:]
                    else:
                        # Client is too far behind: jump to the newest keyframe.
                        frags = [self.fragments[-1]]
                    return frags, self.frag_seq
                if self.closed:
                    return None
                self.cond.wait(timeout=0.3)
            return [], self.frag_seq

    def release_client(self) -> None:
        with self.cond:
            self.clients = max(0, self.clients - 1)

    def stats(self) -> dict[str, Any]:
        with self.cond:
            return {
                "inst": self.inst,
                "short": short_inst(self.inst),
                "width": self.width,
                "height": self.height,
                "source_width": self.source_width,
                "source_height": self.source_height,
                "init": self.init is not None,
                "fragments": self.frag_seq,
                "clients": self.clients,
                "closed": self.closed,
                "frames_in": self.frames_in,
                "bytes_in": self.bytes_in,
                "last_fragment_age": (time.time() - self.last_fragment_at) if self.last_fragment_at else None,
                "stderr": list(self.stderr_tail),
            }

    def stop(self) -> None:
        self._mark_closed()
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


@dataclass
class Instance:
    inst: str
    width: int = 0
    height: int = 0
    extra: Optional[bytes] = None
    nals: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500))
    frames: int = 0
    keys: int = 0
    first_frame: float = 0.0
    last_frame: float = 0.0
    extra_count: int = 0

    @property
    def short(self) -> str:
        return short_inst(self.inst)

    def frames_since(self, since: float) -> int:
        return sum(1 for _, key, ts in self.nals if ts >= since)

    def last_idr_index(self) -> int:
        for i in range(len(self.nals) - 1, -1, -1):
            nal = self.nals[i][0]
            if nal and ((nal[0] >> 1) & 0x3F) in (16, 17, 18, 19, 20, 21):
                return i
        return -1


class CaptureError(RuntimeError):
    pass


class Capture:
    def __init__(self, hook_path: Optional[str] = None, log=print) -> None:
        self.hook_path = hook_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ec4_hook.js")
        self.log = log
        self.lock = threading.RLock()
        self.instances: dict[str, Instance] = {}
        self.session = None
        self.script = None
        self.pid: Optional[int] = None
        self.connected = False
        self.last_error = ""
        self.selected_inst: Optional[str] = None
        self.selected_camera: Optional[str] = None
        self.encoder: Optional[Encoder] = None
        self._closing = False
        self._manager = threading.Thread(target=self._manager_loop, daemon=True)
        self._manager.start()

    # -- connection ---------------------------------------------------------
    @staticmethod
    def _device():
        try:
            return frida.get_usb_device(timeout=4)
        except Exception:
            pass
        for dev in frida.get_device_manager().enumerate_devices():
            name = (dev.name or "").lower()
            if dev.type == "usb" or "emulator" in name or "android" in name:
                return dev
        raise CaptureError("No Android device/emulator found via Frida")

    @staticmethod
    def _find_pid(device) -> Optional[int]:
        procs = device.enumerate_processes()
        for proc in procs:
            if proc.name in APP_NAMES:
                return proc.pid
        for proc in procs:
            if "imilab" in proc.name.lower() and ":" not in proc.name:
                return proc.pid
        return None

    def connect(self) -> bool:
        try:
            device = self._device()
            pid = self._find_pid(device)
            if pid is None:
                raise CaptureError("Imilab Home is not running")
            with open(self.hook_path, "r", encoding="utf-8") as fh:
                source = fh.read()
            session = device.attach(pid)
            script = session.create_script(source)
            script.on("message", self._on_message)
            script.load()
            with self.lock:
                self.session = session
                self.script = script
                self.pid = pid
                self.connected = True
                self.last_error = ""
            self.log(f"capture attached to pid {pid}")
            return True
        except Exception as exc:
            with self.lock:
                self.connected = False
                self.last_error = str(exc)
            return False

    def _detach(self) -> None:
        with self.lock:
            script, session = self.script, self.session
            self.script = None
            self.session = None
            self.connected = False
            self.pid = None
        try:
            if script is not None:
                script.unload()
        except Exception:
            pass
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass

    def _manager_loop(self) -> None:
        while not self._closing:
            try:
                if self.connected and self.pid is not None:
                    # Detect app restart by pid change.
                    device = self._device()
                    pid = self._find_pid(device)
                    if pid is None or pid != self.pid:
                        self.log("app restarted; reconnecting capture")
                        self._detach()
                        with self.lock:
                            self.instances.clear()
                            self._stop_encoder_locked()
                if not self.connected:
                    self.connect()
            except Exception:
                pass
            time.sleep(2.0)

    # -- messages -----------------------------------------------------------
    def _on_message(self, message: dict, data: bytes | None) -> None:
        if message.get("type") == "send":
            payload = message.get("payload", {})
        else:
            payload = message
        typ = payload.get("type")
        if typ == "ready":
            self.log("Frida hook ready")
            return
        if typ not in ("extra", "nal"):
            return
        inst = payload.get("inst", "?")
        now = time.time()
        with self.lock:
            rec = self.instances.get(inst)
            if rec is None:
                rec = Instance(inst=inst, first_frame=now)
                self.instances[inst] = rec
                self.log(f"new decoder {short_inst(inst)}")
            if typ == "extra":
                if data:
                    rec.extra = bytes(data)
                    rec.width = int(payload.get("w", rec.width))
                    rec.height = int(payload.get("h", rec.height))
                    rec.extra_count += 1
                return
            if not data:
                return
            nal = bytes(data)
            is_key = bool(payload.get("key"))
            rec.frames += 1
            if is_key:
                rec.keys += 1
            rec.last_frame = now
            rec.nals.append((nal, is_key, now))
            if inst == self.selected_inst and self.encoder is not None:
                self.encoder.feed(b"\x00\x00\x00\x01" + nal)

    # -- public API ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            insts = []
            for rec in self.instances.values():
                insts.append(
                    {
                        "inst": rec.inst,
                        "short": rec.short,
                        "width": rec.width,
                        "height": rec.height,
                        "extra": bool(rec.extra),
                        "frames": rec.frames,
                        "keys": rec.keys,
                        "last_frame_age": (now - rec.last_frame) if rec.last_frame else None,
                        "fps": rec.frames_since(now - 2.0) / 2.0,
                        "selected": rec.inst == self.selected_inst,
                    }
                )
            insts.sort(key=lambda x: x["frames"], reverse=True)
            encoder = self.encoder.stats() if self.encoder is not None else None
        return {
            "connected": self.connected,
            "pid": self.pid,
            "error": self.last_error,
            "instances": insts,
            "selected_inst": self.selected_inst,
            "selected_camera": self.selected_camera,
            "encoder": encoder,
        }

    def active_instance(self, since: float = 0.0, min_frames: int = 4) -> Optional[Instance]:
        now = time.time()
        best: Optional[Instance] = None
        best_score = -1
        with self.lock:
            for rec in self.instances.values():
                if rec.extra is None or rec.last_frame <= 0:
                    continue
                if now - rec.last_frame > 1.8:
                    continue
                if rec.last_idr_index() < 0:
                    continue
                if rec.first_frame < since - 0.3 and rec.frames_since(since) < min_frames:
                    continue
                score = rec.frames_since(since) if since else rec.frames_since(now - 2.0)
                if rec.last_frame > (best.last_frame if best else 0):
                    score += 1
                if score > best_score:
                    best, best_score = rec, score
        return best

    def wait_for_active(self, since: float, timeout: float = 30.0) -> Optional[Instance]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = self.active_instance(since=since)
            if rec is not None:
                return rec
            time.sleep(0.25)
        return self.active_instance(since=since, min_frames=1)

    def select(self, inst: str) -> bool:
        with self.lock:
            rec = self.instances.get(inst)
            if rec is None or rec.extra is None:
                return False
            idx = rec.last_idr_index()
            if idx < 0:
                return False
            if self.selected_inst == inst and self.encoder is not None and not self.encoder.closed:
                return True
            self._stop_encoder_locked()
            encoder = Encoder(inst, rec.extra, rec.width, rec.height, log=self.log)
            self.encoder = encoder
            self.selected_inst = inst
            # Feed from the most recent IRAP/keyframe.
            nals = list(rec.nals)[idx:]
            for nal, _key, _ts in nals:
                encoder.feed(b"\x00\x00\x00\x01" + nal)
            self.log(f"selected decoder {rec.short} (source {rec.width}x{rec.height}, output {encoder.width}x{encoder.height})")
            return True

    def _stop_encoder_locked(self) -> None:
        encoder = self.encoder
        self.encoder = None
        self.selected_inst = None
        if encoder is not None:
            encoder.stop()

    def stop_encoder(self) -> None:
        with self.lock:
            self._stop_encoder_locked()
            self.selected_camera = None

    def set_camera(self, name: str) -> None:
        with self.lock:
            self.selected_camera = name

    def close(self) -> None:
        self._closing = True
        with self.lock:
            self._stop_encoder_locked()
        self._detach()
