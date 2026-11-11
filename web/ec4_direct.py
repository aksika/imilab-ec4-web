"""Pure-Mac capture backend: no Android app and no emulator.

It obtains the camera session with the app-free stream query, opens the camera
with the private `glive` relay viewer, negotiates signal/ICE from the Mac, and
feeds RTP to the normal ffmpeg encoder.
"""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

import direct_signal  # noqa: E402
import ec4_stun as fit  # noqa: E402
import imi_login  # noqa: E402
import ec4_config  # noqa: E402
from ec4_encoder import CaptureError, Encoder  # noqa: E402
from ec4_rtp import _Receiver, _idr_start  # noqa: E402

VIEWER = os.path.join(TOOLS, "rtmp_private_viewer.py")


class DirectCapture:
    """One camera at a time, pure Mac; switching stops and reopens."""

    def __init__(self, log=print) -> None:
        self.log = log
        self.lock = threading.RLock()
        self.selected_camera = None
        self.selected_inst = None
        self.encoder: Encoder | None = None
        self.receiver: _Receiver | None = None
        self.viewer: subprocess.Popen | None = None
        self.connected = False
        self.mode = "direct"
        self.session_data: dict | None = None
        self.first_event = threading.Event()
        self.params = b""
        self.first_idr = b""
        self.seen_first = False
        self.last_keyframe = None
        self.queue: collections.deque[bytes] = collections.deque(maxlen=240)
        self.queue_lock = threading.Lock()
        self.feed_target = None
        self.feed_lock = threading.Lock()
        self.feeder_started = False
        self._first_rtp = 0
        self._first_checks = 0

    # -- media callbacks ---------------------------------------------------
    def _on_keyframe(self, data: bytes) -> None:
        self.last_keyframe = data
        if not self.seen_first:
            j = _idr_start(data)
            if j < 0:
                return
            self.params = data[: j + 4]
            self.first_idr = data[j + 4:]
            self.seen_first = True
            self.first_event.set()
            return
        with self.queue_lock:
            self.queue.append(data)

    def _on_delta(self, data: bytes, nal_type: int) -> None:
        if not self.seen_first:
            return
        with self.queue_lock:
            self.queue.append(data)

    def _feed_loop(self) -> None:
        while True:
            with self.feed_lock:
                target = self.feed_target
            if target is None:
                time.sleep(0.05)
                continue
            with self.queue_lock:
                batch = list(self.queue)
                self.queue.clear()
            for item in batch:
                try:
                    target.feed(item)
                except Exception:
                    pass
            time.sleep(0.03)

    def _attach_encoder(self) -> None:
        encoder = Encoder("direct", self.params, 1280, 720, log=self.log)
        encoder.feed(self.first_idr)
        self.encoder = encoder
        with self.feed_lock:
            self.feed_target = encoder
        if not self.feeder_started:
            self.feeder_started = True
            threading.Thread(target=self._feed_loop, daemon=True).start()
        self.connected = True
        self.selected_inst = "direct"
        self.log("Direct media: encoder started")

    # -- lifecycle ---------------------------------------------------------
    def _stop_stream(self) -> None:
        with self.feed_lock:
            self.feed_target = None
        if self.encoder is not None:
            try:
                self.encoder.stop()
            except Exception:
                pass
            self.encoder = None
        if self.receiver is not None:
            self.receiver.stop_flag.set()
            self.receiver.join(timeout=2)
            self.receiver = None
        if self.viewer is not None:
            try:
                self.viewer.terminate()
                self.viewer.wait(timeout=3)
            except Exception:
                try:
                    self.viewer.kill()
                except Exception:
                    pass
            self.viewer = None
        self.connected = False
        self.selected_inst = None
        self.seen_first = False
        self.first_event.clear()
        with self.queue_lock:
            self.queue.clear()

    def _start_viewer(self, relay: str, iot_id: str) -> None:
        env = os.environ.copy()
        env["EC4_RELAY_URL"] = relay
        env["EC4_RELAY_PING_EARLY"] = "1"
        env["EC4_IOT_ID"] = iot_id
        log_path = "/tmp/ec4_direct_viewer.log"
        try:
            fh = open(log_path, "ab", buffering=0)
        except Exception:
            fh = subprocess.DEVNULL
        self.viewer = subprocess.Popen(
            [sys.executable, VIEWER, "3600"],
            stdout=fh, stderr=fh, env=env,
        )
        self.log(f"relay viewer pid {self.viewer.pid}")

    def _wait_active(self, iot: str, iot_id: str, timeout: float = 25.0) -> None:
        import p2p_media
        deadline = time.time() + timeout
        while time.time() < deadline:
            if p2p_media.camera_active(iot, iot_id) == 1:
                self.log("camera active via relay")
                return
            time.sleep(1.0)
        raise CaptureError("camera did not become active via relay viewer")

    def select(self, name: str, timeout: float = 90.0) -> dict:
        name = name.strip()
        if not name:
            raise CaptureError("camera name is required")
        with self.lock:
            if (
                self.selected_camera == name
                and self.encoder is not None
                and not self.encoder.closed
            ):
                return self.status()
            self._stop_stream()
            iot_id = ec4_config.camera_ids().get(name) or direct_signal.IOT_ID
            try:
                imi_login.ensure_iot_token()
            except Exception as exc:
                self.log(f"IoT token check failed: {exc}")
            try:
                data, signal, stun, sess, iot = direct_signal.query_session(iot_id)
                relay = data.get("relayUrl") or ""
                if not relay:
                    raise CaptureError("stream query returned no relayUrl")
                self.selected_camera = name
                self._start_viewer(relay, iot_id)
                self._wait_active(iot, iot_id)
                time.sleep(1.0)
                self.log("negotiating P2P signal")
                creds = direct_signal.negotiate_sync(signal, stun, sess, iot, self.log)
                payload = (
                    '{"version":"2.0","seq_num":2,"type":"request","params":'
                    '{"messageType":"startStreaming","streamType":0,'
                    '"sessionId":"%s"}}' % sess
                )
                remote = next(
                    (p for p in creds["pws"] if fit.verify(creds["p100"], p)), None
                )
                if not remote:
                    raise CaptureError("could not verify camera ICE password")
                start_packet = fit.build_data_request(
                    creds["p100"], payload, "2", remote
                )
                if not start_packet:
                    raise CaptureError("could not build startStreaming")
                self.receiver = _Receiver(
                    creds["hub"], creds["pws"], creds["p100"], start_packet,
                    self._on_keyframe, self._on_delta, log=self.log,
                )
                self.receiver.start()
                if not self.first_event.wait(timeout):
                    r = self.receiver
                    raise CaptureError(
                        "no keyframe from camera "
                        f"(rtp={getattr(r, 'rtp', 0)}, checks={getattr(r, 'checks_answered', 0)})"
                    )
                self._attach_encoder()
                self.session_data = creds
                self.log(
                    f"DIRECT {name} live: {self.encoder.stats().get('width', 0)}x"
                    f"{self.encoder.stats().get('height', 0)}"
                )
                return self.status()
            except Exception:
                self._stop_stream()
                raise

    def switch(self, name: str, timeout: float = 30.0) -> dict:
        return self.select(name, timeout)

    def stop(self) -> None:
        with self.lock:
            self._stop_stream()
            self.selected_camera = None

    def close(self) -> None:
        self.stop()

    def status(self) -> dict:
        with self.lock:
            st = self.encoder.stats() if self.encoder is not None else {}
            r = self.receiver
            st.update({
                "mode": "direct",
                "selected_camera": self.selected_camera,
                "selected_inst": self.selected_inst,
                "connected": bool(self.encoder is not None),
                "connected_app": False,
                "broker": False,
                "instances": (
                    [{
                        "inst": "direct",
                        "short": "direct",
                        "frames": st.get("frames_in", 0),
                        "fps": 0.0,
                        "width": st.get("width", 0),
                        "height": st.get("height", 0),
                        "extra": True,
                        "keyframe": True,
                    }] if self.encoder is not None else []
                ),
                "session": (
                    {
                        "camera": self.selected_camera,
                        "rtp": r.rtp,
                        "keyframes": r.keyframes,
                        "deltas": r.deltas,
                        "seq_gaps": r.seq_gaps,
                        "seq_lost": r.seq_lost,
                        "key": (
                            r.aes_key.decode("ascii", "replace")
                            if r.aes_key else None
                        ),
                    } if r is not None else None
                ),
            })
            return st
