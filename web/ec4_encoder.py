"""H.265 -> browser fragmented-MP4 encoder for the direct path.

The encoder is independent of the Android app/emulator.  It receives Annex-B
H.265 access units from ec4_rtp and writes browser-friendly fragmented MP4.

Two output modes are supported:

* ``h264`` (default) decodes and re-encodes with libx264.
* ``h265`` copies the camera's HEVC stream into fMP4 without re-encoding.  The
  browser must support HEVC in Media Source Extensions.
"""
from __future__ import annotations

import collections
import os
import shutil
import subprocess
import threading
import time
from typing import Optional

FFMPEG = shutil.which("ffmpeg")

# Browser MSE hints.  The browser derives the exact string from the fMP4 init
# segment, so the HEVC value only needs to be a valid Main-profile fallback.
H264_CODEC = "avc1.42C028"  # constrained baseline, level 4.0
H265_CODEC = "hvc1.1.6.L120.B0"  # Main profile, level 4.0
MSE_CODEC = H264_CODEC  # backwards-compatible default

CODECS = ("h264", "h265")


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


def ffmpeg_cmd(codec: str = "h264") -> list[str]:
    if not FFMPEG:
        raise RuntimeError("ffmpeg not found. Install it with: brew install ffmpeg")
    if codec not in CODECS:
        raise ValueError(f"unsupported codec: {codec}")

    command = [
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
    ]
    if codec == "h265":
        # Remux only: keep the camera's HEVC bitstream and let the browser
        # hardware-decode it.  hvc1 is the tag Safari/Chrome expect in fMP4.
        command += [
            "-c:v", "copy",
            "-tag:v", "hvc1",
        ]
    else:
        command += [
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
        ]
    command += [
        "-fps_mode", "vfr",
        "-f", "mp4",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-flush_packets", "1",
        "pipe:1",
    ]
    return command


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

    def __init__(
        self,
        inst: str,
        extra: bytes,
        width: int,
        height: int,
        log=print,
        codec: str = "h264",
    ) -> None:
        if codec not in CODECS:
            raise ValueError(f"unsupported codec: {codec}")
        self.inst = inst
        self.extra = extra
        self.source_width = width
        self.source_height = height
        if codec == "h265":
            # Copy mode cannot scale; keep the camera resolution.
            self.width, self.height = width, height
        else:
            self.width, self.height = output_size(width, height)
        self.codec = codec
        self.passthrough = codec == "h265"
        self.codec_string = H265_CODEC if self.passthrough else H264_CODEC
        self.log = log

        self.proc = subprocess.Popen(
            ffmpeg_cmd(codec),
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
            mode = "H.265 passthrough" if self.passthrough else "H.264"
            self.log(
                f"encoder {short_inst(inst)} {self.width}x{self.height} "
                f"(source {width}x{height}, {mode}) started"
            )

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
                "codec": self.codec,
                "codec_string": self.codec_string,
                "passthrough": self.passthrough,
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


class CaptureError(RuntimeError):
    pass
