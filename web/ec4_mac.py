"""Mac-side Link Visual receiver.

The Imilab app is used only as the cloud/session broker.  On a cold start the
backend wakes the camera through the IoT property API, creates the app's own
native `LVLivePlayer` through Frida (no UI taps), neutralises the app's
`startStreaming`, and lets the Mac take over the media transport.  The Mac
decrypts the 0xbb keyframe fragments, reassembles the H.265 access units, and
feeds them to ffmpeg.

Key facts used here:
  * 0x09 fragments are plaintext: payload[2:] is access-unit data.
  * 0xbb fragments are AES-128-CBC (IV=0): payload[4:] is ciphertext rounded
    down to a multiple of 16, followed by a 3-byte plaintext tail.  The key is
    the first 16 bytes of the video section's ICE password.
  * The access unit is the VPS/SPS/PPS parameter sets plus the IDR NAL,
    reassembled in RTP sequence order.
  * 0x0a starts a video NAL (skip 6 bytes), 0x08 continues it (skip 2).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import zlib
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from queue import Queue, Empty

import frida

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(WEB_DIR, "..", "tools")
sys.path.insert(0, TOOLS_DIR)
import force_iframe_test as fit  # noqa: E402
import ec4_config  # noqa: E402

from ec4_capture import CaptureError, Encoder  # noqa: E402

APP = "com.chuangmi.imihome"
ADB = os.environ.get("EC4_ADB") or "adb"
NEUTRALIZE_JS = os.path.join(TOOLS_DIR, "session_hooks_neutralize.js")
PLAYER_BRIDGE_JS = os.path.join(TOOLS_DIR, "frida_player_bridge.bundle.js")
SESSION_CACHE = os.path.join(WEB_DIR, "sessions.json")

# Camera nickname -> Alibaba IoT device id (used for the wake property and the
# headless Link Visual player).
IOT_IDS = ec4_config.camera_ids()
SESSION_TTL = int(os.environ.get("EC4_SESSION_TTL", "1800"))
STUN_MAGIC = b"\x21\x12\xa4\x42"


def _rebuild_binding(template: bytes, pw: str) -> bytes:
    """Rebuild a STUN binding request with a fresh transaction ID."""
    try:
        r = fit.parse(template)
        if not r:
            return template
        _, attrs = r
        body = bytearray()
        for t, l, v, off in attrs:
            if t in (0x0008, 0x8028):
                continue
            pad = (4 - (l % 4)) % 4
            body += struct.pack(">HH", t, l) + v + b"\x00" * pad
        txid = os.urandom(12)
        hdr = struct.pack(">HH", 0x0001, len(body) + 24 - 20) + STUN_MAGIC + txid
        mi = hmac.new(pw.encode(), hdr + body, hashlib.sha1).digest()
        body += struct.pack(">HH", 0x0008, 20) + mi
        full_len = 20 + len(body) + 8
        hdr = struct.pack(">HH", 0x0001, full_len - 20) + STUN_MAGIC + txid
        crc = (zlib.crc32(hdr + body) & 0xFFFFFFFF) ^ 0x5354554E
        body += struct.pack(">HHI", 0x8028, 4, crc)
        return hdr + body
    except Exception:
        return template


def _key_candidates(pws: list[str]) -> list[bytes]:
    """The AES key is the first 16 bytes of one of the session ICE passwords."""
    out: list[bytes] = []
    for pw in pws:
        if len(pw) >= 16:
            key = pw[:16].encode("ascii", "ignore")
            if len(key) == 16 and key not in out:
                out.append(key)
    return out


class _Receiver(threading.Thread):
    """Receives RTP from the camera and emits Annex-B H.265 access units."""

    def __init__(
        self,
        hub: tuple[str, int],
        pws: list[str],
        p100: bytes,
        start_packet: bytes,
        on_keyframe,
        on_delta,
        log=print,
    ) -> None:
        super().__init__(daemon=True)
        self.hub = hub
        self.pws = pws
        self.p100 = p100
        self.start_packet = start_packet
        self.on_keyframe = on_keyframe
        self.on_delta = on_delta
        self.log = log

        self.stop_flag = threading.Event()
        self.sock: socket.socket | None = None
        self.local_pw: str | None = None
        self.remote_pw: str | None = None
        self.aes_key: bytes | None = None
        self.candidates = _key_candidates(pws)
        self.got_success = False
        self.checks_answered = 0
        self.first_check_time = 0.0
        self.error = ""
        self.rtp = 0
        self.keyframes = 0
        self.deltas = 0
        self.media_ssrc: int | None = None

        # delta NAL reassembly
        self._nal = bytearray()
        self._nal_started = False

        # keyframe fragment buffers keyed by RTP timestamp
        self._kf: dict[int, list[tuple[int, bytes]]] = {}
        self._kf_last: dict[int, float] = {}

        # RTP is processed on a worker so the UDP socket is always drained.
        self._raw: Queue[bytes] = Queue(maxsize=8192)
        self._worker: threading.Thread | None = None
        self.last_seq: dict[int, int] = {}
        self.seq_gaps = 0
        self.seq_lost = 0

    # -- crypto ------------------------------------------------------------
    def _decrypt_bb(self, body: bytes) -> bytes | None:
        aes_len = (len(body) // 16) * 16
        if aes_len < 16:
            return None
        if self.aes_key is None:
            # Try every session password; the right one yields 0b00/0900.
            for key in self.candidates:
                plain = _aes_cbc(key, body[:aes_len])
                if plain[:2] in (b"\x0b\x00", b"\x09\x00"):
                    self.aes_key = key
                    self.log(f"media key {key.decode('ascii', 'replace')}")
                    return plain[2:] + body[aes_len:]
            return None
        return _aes_cbc(self.aes_key, body[:aes_len])[2:] + body[aes_len:]

    # -- RTP ---------------------------------------------------------------
    def _finish_nal(self) -> None:
        if self._nal_started and self._nal:
            data = bytes(self._nal)
            nal_type = (data[0] >> 1) & 0x3F
            self.on_delta(b"\x00\x00\x00\x01" + data, nal_type)
            self.deltas += 1
        self._nal = bytearray()
        self._nal_started = False

    def _flush_keyframe(self, rt: int) -> None:
        frags = self._kf.pop(rt, None)
        self._kf_last.pop(rt, None)
        if not frags:
            return
        frags.sort(key=lambda x: x[0])
        data = b"".join(f for _, f in frags)
        # first fragment starts with 0b00 + VPS/SPS/PPS + start code + IDR
        self.keyframes += 1
        self.on_keyframe(data)
        self.log(f"keyframe rt={rt} fragments={len(frags)} bytes={len(data)}")

    def _rtp(self, d: bytes) -> None:
        self.rtp += 1
        seq = int.from_bytes(d[2:4], "big")
        rt = int.from_bytes(d[4:8], "big")
        ssrc = int.from_bytes(d[8:12], "big")
        prev = self.last_seq.get(ssrc)
        if prev is not None:
            delta = (seq - prev) & 0xFFFF
            if delta == 0 or delta > 30000:
                pass
            elif delta > 1:
                self.seq_gaps += 1
                self.seq_lost += delta - 1
        self.last_seq[ssrc] = seq
        if self.media_ssrc is None:
            self.media_ssrc = ssrc
            self.log(f"media ssrc {ssrc:#x}")
        marker = bool(d[1] & 0x80)
        payload = d[12:]
        if not payload:
            return
        h = payload[0]

        if h == 0x0A:
            self._finish_nal()
            self._nal = bytearray(payload[6:]) if len(payload) > 6 else bytearray()
            self._nal_started = True
            if marker:
                self._finish_nal()
            return
        if h == 0x08:
            if self._nal_started and len(payload) > 2:
                self._nal += payload[2:]
            if marker:
                self._finish_nal()
            return
        if h == 0x09:
            frag = payload[2:]
        elif h == 0xBB:
            frag = self._decrypt_bb(payload[4:])
            if frag is None:
                return
        else:
            return

        self._kf.setdefault(rt, []).append((seq, frag))
        self._kf_last[rt] = time.time()
        if marker:
            self._flush_keyframe(rt)

    def _worker_loop(self) -> None:
        while not self.stop_flag.is_set():
            try:
                d = self._raw.get(timeout=0.2)
            except Empty:
                for rt, last in list(self._kf_last.items()):
                    if time.time() - last > 0.4:
                        self._flush_keyframe(rt)
                continue
            try:
                self._rtp(d)
            except Exception:
                pass

    # -- main --------------------------------------------------------------
    def run(self) -> None:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(0.2)
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            except Exception:
                pass
            self.sock.connect(self.hub)
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()
            lip, lport = self.sock.getsockname()
            self.log(f"mac media socket {lip}:{lport}")
            self.sock.send(self.p100)
            start = time.time()
            sent_start = False
            while not self.stop_flag.is_set() and time.time() - start < 3600:
                now = time.time()
                ready = (
                    self.got_success
                    and self.checks_answered >= 1
                    and self.first_check_time
                    and (now - self.first_check_time) > 0.5
                )
                if not sent_start and (ready or now - start > 2.5):
                    self.sock.send(self.start_packet)
                    self.log("Mac startStreaming sent")
                    sent_start = True
                try:
                    d = self.sock.recv(65535)
                except socket.timeout:
                    continue
                except Exception as exc:
                    self.error = str(exc)
                    break
                r = fit.parse(d)
                if r:
                    typ, _ = r
                    if typ == 1:
                        if self.local_pw is None:
                            self.local_pw = next(
                                (p for p in self.pws if fit.verify(d, p)), None
                            )
                        if self.local_pw:
                            try:
                                self.sock.send(
                                    fit.success(d[8:20], lip, lport, self.local_pw)
                                )
                                self.checks_answered += 1
                                if not self.first_check_time:
                                    self.first_check_time = time.time()
                            except Exception:
                                pass
                    elif typ in (0x0101, 0x0111):
                        self.got_success = True
                    continue
                if len(d) >= 12 and (d[0] >> 6) == 2:
                    try:
                        self._raw.put_nowait(d)
                    except Exception:
                        pass
        except Exception as exc:
            self.error = str(exc)
        finally:
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass


def _aes_cbc(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    dec = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()
    return dec.update(data) + dec.finalize()


class _Session:
    """One long-lived app-brokered session; cameras can be switched without respawn."""

    def __init__(self, adb, camera: str, log=print) -> None:
        self.adb = adb
        self.camera = camera
        self.log = log
        self.frida = None
        self.script = None
        self.player_bridge = None
        self.pid = 0
        self.app_alive = False
        self.hooks_installed = False
        self.receiver: _Receiver | None = None
        self.blocked = False
        self.broad_blocked = False
        self.lines: deque[str] = deque(maxlen=8000)
        self.lines_lock = threading.Lock()
        self.session_id = ""
        self.hub: tuple[str, int] | None = None
        self.pws: list[str] = []
        self.p100: bytes | None = None
        self.first_keyframe: bytes | None = None
        self.first_idr: bytes | None = None
        self.params: bytes = b""
        self.first_kf_event = threading.Event()
        self.queue: deque[bytes] = deque(maxlen=240)
        self.queue_lock = threading.Lock()
        self.seen_first = False
        self.last_keyframe: bytes | None = None
        self.keyframe_seq = 0
        self.feed_target = None
        self.feed_lock = threading.Lock()
        self.feeder_started = False
        self.error = ""

    # -- frida -------------------------------------------------------------
    def _on_message(self, message, data) -> None:
        if message.get("type") == "send":
            payload = message.get("payload") or {}
            if "__log" in payload:
                line = str(payload["__log"])
                with self.lines_lock:
                    self.lines.append(line)
                return
            if "log" in payload:
                line = "player: " + str(payload["log"])
                with self.lines_lock:
                    self.lines.append(line)
                return
        elif message.get("type") == "log":
            with self.lines_lock:
                self.lines.append(str(message.get("payload", "")))

    def _snapshot(self) -> list[str]:
        with self.lines_lock:
            return list(self.lines)

    def _find_state(self, since: int = 0, exclude_ip: str | None = None):
        p100_by_dest: dict[tuple[str, int], bytes] = {}
        p100_last = None
        p256_dest = None
        pws: list[str] = []
        sess_id = None
        for line in self._snapshot()[since:]:
            m = re.search(
                r"\[NET\] sendto dest=2:([\d.]+):(\d+) len=100 hex=([0-9a-f]+)", line
            )
            if m:
                dest = (m.group(1), int(m.group(2)))
                pkt = bytes.fromhex(m.group(3))
                p100_by_dest[dest] = pkt
                p100_last = (dest[0], dest[1], pkt)
            m = re.search(
                r"\[NET\] sendto dest=2:([\d.]+):(\d+) len=256 hex=([0-9a-f]+)", line
            )
            if m:
                p256_dest = (m.group(1), int(m.group(2)))
            m = re.search(r"MI pwd=([\d,]+)", line)
            if m:
                try:
                    pw = bytes(int(x) for x in m.group(1).split(",") if x).decode(
                        "ascii", "replace"
                    )
                    if len(pw) >= 20 and pw not in pws:
                        pws.append(pw)
                except Exception:
                    pass
            m = re.search(r"\[SESS\] sessionId=([\d,]+|\S+)$", line)
            if m and m.group(1) not in ("", "undefined", "null"):
                raw = m.group(1)
                if "," in raw:
                    try:
                        raw = bytes(int(x) for x in raw.split(",") if x).decode(
                            "ascii", "replace"
                        )
                    except Exception:
                        pass
                sess_id = raw
        p100 = None
        if (
            p256_dest in p100_by_dest
            and (not exclude_ip or p256_dest[0] != exclude_ip)
        ):
            p100 = (p256_dest[0], p256_dest[1], p100_by_dest[p256_dest])
        else:
            for dest, pkt in reversed(list(p100_by_dest.items())):
                if not dest[0].startswith("192.168."):
                    continue
                if exclude_ip and dest[0] == exclude_ip:
                    continue
                p100 = (dest[0], dest[1], pkt)
                break
        if p100 is None and p100_last and p100_last[0].startswith("192.168."):
            if not exclude_ip or p100_last[0] != exclude_ip:
                p100 = p100_last
        return p100, pws, sess_id

    # -- media callbacks ---------------------------------------------------
    def _on_keyframe(self, data: bytes) -> None:
        self.keyframe_seq += 1
        self.last_keyframe = data
        if not self.seen_first:
            j = _idr_start(data)
            if j < 0:
                return
            self.params = data[: j + 4]
            self.first_idr = data[j + 4 :]
            self.first_keyframe = data
            self.seen_first = True
            self.first_kf_event.set()
        else:
            with self.queue_lock:
                self.queue.append(data)

    def _on_delta(self, data: bytes, nal_type: int) -> None:
        if not self.seen_first:
            return
        with self.queue_lock:
            self.queue.append(data)

    def clear_queue(self) -> None:
        with self.queue_lock:
            self.queue.clear()

    def set_target(self, encoder) -> None:
        with self.feed_lock:
            self.feed_target = encoder
        if encoder is not None and not self.feeder_started:
            self.feeder_started = True
            threading.Thread(target=self._feed_loop, daemon=True).start()

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
            time.sleep(0.05)

    # -- app / session -----------------------------------------------------
    def _start_app(self) -> None:
        if self.app_alive:
            return
        subprocess.run([ADB, "shell", "am", "force-stop", APP], check=False)
        time.sleep(1)
        dev = frida.get_usb_device(timeout=10)
        pid = dev.spawn([APP])
        self.pid = pid
        self.log(f"spawned app {pid}")
        self.frida = dev.attach(pid)
        self.script = self.frida.create_script(
            open(NEUTRALIZE_JS, encoding="utf-8").read()
        )
        self.script.on("message", self._on_message)
        self.script.load()
        dev.resume(pid)
        time.sleep(9)
        self.app_alive = True
        self.hooks_installed = False
        try:
            self.player_bridge = self.frida.create_script(
                open(PLAYER_BRIDGE_JS, encoding="utf-8").read()
            )
            self.player_bridge.on("message", self._on_message)
            self.player_bridge.load()
            self.log("direct player bridge loaded")
        except Exception as exc:
            self.player_bridge = None
            self.log(f"direct player bridge unavailable: {exc}")

    def _wake_camera(self, camera: str) -> None:
        """Bring a battery camera out of deep sleep before starting P2P."""
        iot_id = ec4_config.camera_ids().get(camera) or IOT_IDS.get(camera)
        if not iot_id:
            return
        try:
            import iot_auth

            creds = iot_auth.load_creds()
            if creds.get("refreshToken") and creds.get("identityId"):
                res = iot_auth.refresh(creds["identityId"], creds["refreshToken"])
                if res["status"] == 200:
                    creds.update(json.loads(res["body"]).get("data") or {})
                    iot_auth.save_creds(creds)
            iot = creds.get("iotToken")
            if not iot:
                self.log(f"wake {camera}: no IoT token")
                return
            res = iot_auth.thing_properties_set(
                iot_id, iot, {"SleepStatus": 1}
            )
            self.log(f"wake {camera}: {res['status']}")
            deadline = time.time() + 20.0
            while time.time() < deadline:
                time.sleep(1.0)
                r = iot_auth.thing_properties_get(iot_id, iot)
                if r["status"] != 200:
                    continue
                data = json.loads(r["body"]).get("data") or {}
                if (data.get("ActiveStatus") or {}).get("value") == 1:
                    self.log(f"{camera} active")
                    return
            self.log(f"wake {camera}: ActiveStatus did not become 1")
        except Exception as exc:
            self.log(f"wake {camera} failed: {exc}")

    def _install_hooks(self) -> None:
        installed = False
        for i in range(3000):
            try:
                if self.script.exports_sync.install():
                    installed = True
                    self.log(f"hooks installed at {i}")
                    break
            except Exception:
                pass
            time.sleep(0.02)
        if not installed:
            raise CaptureError(
                "Frida hooks did not install; is frida-server running?"
            )
        self.hooks_installed = True

    def _start_direct_player(self, camera: str) -> bool:
        """Start the app's native player through Frida; no UI taps."""
        if self.player_bridge is None:
            return False
        iot_id = ec4_config.camera_ids().get(camera) or IOT_IDS.get(camera)
        if not iot_id:
            return False
        try:
            self.player_bridge.exports_sync.stop()
        except Exception:
            pass
        self._wake_camera(camera)
        try:
            r = self.player_bridge.exports_sync.prepare()
            if r != "ok":
                self.log(f"direct player prepare failed: {r}")
                return False
            if not self.hooks_installed:
                self._install_hooks()
            r = self.player_bridge.exports_sync.play(iot_id)
            if r != "ok":
                self.log(f"direct player play failed: {r}")
                return False
            self.log(f"direct player started {camera}")
            return True
        except Exception as exc:
            self.log(f"direct player exception: {exc}")
            return False

    def _capture_state(
        self, camera: str, since: int, old_sess: str, old_hub_ip: str | None = None
    ) -> None:
        direct = self._start_direct_player(camera)
        if not direct:
            if self.camera and self.camera != camera:
                try:
                    old = self.adb.find_camera(self.camera)
                    if old and old.get("pause"):
                        b = old["pause"]
                        self.adb.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
                        self.log(f"paused old camera {self.camera}")
                        time.sleep(2)
                except Exception as exc:
                    self.log(f"pause old camera failed: {exc}")
            info = self.adb.play(camera)
            self.log(f"play {camera}: {info}")
            if not self.hooks_installed:
                self._install_hooks()
        else:
            info = {"already": False}
        p100 = None
        pws: list[str] = []
        sess_id = None
        deadline = time.time() + 55
        fallback_at = time.time() + 18 if info.get("already") else 0
        last_log = 0.0
        while time.time() < deadline:
            p100, pws, sess_id = self._find_state(since, old_hub_ip)
            if time.time() - last_log > 2.0:
                self.log(
                    "state p100=%s sess=%s exclude=%s"
                    % (p100[:2] if p100 else None, sess_id, old_hub_ip)
                )
                last_log = time.time()
            if p100 and len(pws) >= 2 and sess_id and sess_id != old_sess:
                break
            if fallback_at and time.time() > fallback_at:
                p100, pws, sess_id = self._find_state(0, old_hub_ip)
                if p100 and len(pws) >= 2 and sess_id:
                    break
            time.sleep(0.02)
        if not p100 or len(pws) < 2 or not sess_id:
            raise CaptureError("could not capture the Link Visual session credentials")
        self.camera = camera
        self.hub = (p100[0], p100[1])
        self.pws = pws
        self.p100 = p100[2]
        self.session_id = sess_id
        self.log(f"hub {self.hub} session={sess_id}")

    def _start_receiver(self, timeout: float) -> None:
        if not self.hub or not self.p100 or not self.pws or not self.session_id:
            raise CaptureError("incomplete session data")
        remote = next((p for p in self.pws if fit.verify(self.p100, p)), None)
        if not remote:
            raise CaptureError("could not verify the camera ICE password")
        payload = (
            '{"version":"2.0","seq_num":2,"type":"request","params":'
            '{"messageType":"startStreaming","streamType":0,"sessionId":"%s"}}'
            % self.session_id
        )
        start_packet = fit.build_data_request(self.p100, payload, "2", remote)
        if not start_packet:
            raise CaptureError("could not build the startStreaming request")
        fresh_p100 = _rebuild_binding(self.p100, remote)
        self.receiver = _Receiver(
            self.hub,
            self.pws,
            fresh_p100,
            start_packet,
            self._on_keyframe,
            self._on_delta,
            log=self.log,
        )
        self.receiver.start()
        if not self.broad_blocked:
            cmd = (
                "su 0 iptables -I OUTPUT -p udp --dport %d -m string --algo bm "
                "--hex-string '|2112a442|' -j DROP" % self.hub[1]
            )
            subprocess.run([ADB, "shell", cmd], capture_output=True, text=True)
            self.blocked = True
        dl = time.time() + timeout
        while time.time() < dl and not self.first_kf_event.is_set():
            if self.receiver.error:
                self.log(f"receiver error: {self.receiver.error}")
                break
            time.sleep(0.05)
        if not self.first_kf_event.is_set():
            raise CaptureError(
                "no decodable keyframe reached the Mac "
                f"(rtp={self.receiver.rtp} deltas={self.receiver.deltas})"
            )
        self.log(
            f"first keyframe: {len(self.first_keyframe)}B, "
            f"params {len(self.params)}B, rtp {self.receiver.rtp}"
        )

    def _block_all_stun(self) -> None:
        if self.broad_blocked:
            return
        cmd = (
            "su 0 iptables -I OUTPUT -p udp -m string --algo bm "
            "--hex-string '|2112a442|' -j DROP"
        )
        subprocess.run([ADB, "shell", cmd], capture_output=True, text=True)
        self.broad_blocked = True
        self.log("app STUN blocked globally for camera switch")

    def _unblock_all_stun(self) -> None:
        if not self.broad_blocked:
            return
        cmd = (
            "su 0 iptables -D OUTPUT -p udp -m string --algo bm "
            "--hex-string '|2112a442|' -j DROP"
        )
        subprocess.run([ADB, "shell", cmd], capture_output=True, text=True)
        self.broad_blocked = False

    def _stop_receiver(self) -> None:
        if self.receiver:
            self.receiver.stop_flag.set()
            self.receiver.join(timeout=2)
            self.receiver = None
        if self.blocked and self.hub:
            cmd = (
                "su 0 iptables -D OUTPUT -p udp --dport %d -m string --algo bm "
                "--hex-string '|2112a442|' -j DROP" % self.hub[1]
            )
            subprocess.run([ADB, "shell", cmd], capture_output=True, text=True)
            self.blocked = False

    def session_data(self) -> dict:
        return {
            "camera": self.camera,
            "hub": list(self.hub or ("", 0)),
            "pws": list(self.pws),
            "p100": self.p100.hex() if self.p100 else "",
            "session_id": self.session_id,
            "saved_at": time.time(),
        }

    def _reset_media(self) -> None:
        self.first_keyframe = None
        self.first_idr = None
        self.params = b""
        self.seen_first = False
        self.first_kf_event.clear()
        self.last_keyframe = None
        self.keyframe_seq = 0
        self.clear_queue()

    def start(self, timeout: float = 90.0) -> None:
        self._start_app()
        mark = len(self._snapshot())
        self._capture_state(self.camera, mark, "", None)
        self._reset_media()
        self._start_receiver(timeout)

    def switch(self, camera: str, timeout: float = 25.0) -> None:
        if not self.app_alive:
            self.camera = camera
            self.start(timeout)
            return
        self._stop_receiver()
        self._block_all_stun()
        self._reset_media()
        old_hub_ip = self.hub[0] if self.hub else None
        mark = len(self._snapshot())
        self._capture_state(camera, mark, self.session_id, old_hub_ip)
        self._start_receiver(timeout)

    def start_cached(self, data: dict, timeout: float = 15.0) -> None:
        self._stop_receiver()
        self.hub = (data["hub"][0], int(data["hub"][1]))
        self.pws = list(data.get("pws") or [])
        self.p100 = bytes.fromhex(data.get("p100") or "")
        self.session_id = data.get("session_id") or ""
        self.camera = data.get("camera") or self.camera
        self._reset_media()
        self._start_receiver(timeout)

    def stop(self, force_stop: bool = False) -> None:
        self._stop_receiver()
        self._unblock_all_stun()
        if self.player_bridge is not None:
            try:
                self.player_bridge.exports_sync.stop()
            except Exception:
                pass
        if force_stop:
            if self.frida:
                try:
                    self.frida.detach()
                except Exception:
                    pass
                self.frida = None
                self.player_bridge = None
            try:
                subprocess.run([ADB, "shell", "am", "force-stop", APP], check=False)
            except Exception:
                pass
            self.app_alive = False


def _idr_start(data: bytes) -> int:
    """Index of the 4-byte start code before the first type 19/20 NAL."""
    pos = 0
    while True:
        j = data.find(b"\x00\x00\x00\x01", pos)
        if j < 0 or j + 5 > len(data):
            return -1
        if ((data[j + 4] >> 1) & 0x3F) in (19, 20):
            return j
        pos = j + 4


class MacCapture:
    """Website backend using the Mac P2P media path and a persistent app broker."""

    def __init__(self, adb, log=print) -> None:
        self.adb = adb
        self.log = log
        self.lock = threading.RLock()
        self.selected_camera = None
        self.selected_inst = None
        self.encoder: Encoder | None = None
        self.session: _Session | None = None
        self.connected = False
        self.mode = "mac"
        self.cache = self._load_cache()
        self.warm_switch = os.environ.get("EC4_WARM_SWITCH", "0") == "1"

    # -- session cache -----------------------------------------------------
    def _load_cache(self) -> dict:
        try:
            with open(SESSION_CACHE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(SESSION_CACHE), exist_ok=True)
            tmp = SESSION_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SESSION_CACHE)
        except Exception as exc:
            self.log(f"session cache save failed: {exc}")

    def _cache_get(self, name: str) -> dict | None:
        entry = self.cache.get(name)
        if not isinstance(entry, dict):
            return None
        age = time.time() - float(entry.get("saved_at") or 0)
        if age > SESSION_TTL:
            self.cache.pop(name, None)
            self._save_cache()
            return None
        return entry

    def _cache_put(self, name: str, session: _Session) -> None:
        self.cache[name] = session.session_data()
        self._save_cache()

    def _cache_drop(self, name: str) -> None:
        if self.cache.pop(name, None) is not None:
            self._save_cache()

    # -- encoder -----------------------------------------------------------
    def stop_encoder(self) -> None:
        if self.encoder is not None:
            try:
                self.encoder.stop()
            except Exception:
                pass
            self.encoder = None
        if self.session is not None:
            self.session.set_target(None)
        self.selected_inst = None

    def _attach_encoder(self, session: _Session, reattach: bool = False) -> None:
        if reattach:
            seq0 = session.keyframe_seq
            deadline = time.time() + 6.0
            while session.keyframe_seq <= seq0 and time.time() < deadline:
                time.sleep(0.05)
            if session.keyframe_seq <= seq0 or not session.last_keyframe:
                raise CaptureError("no keyframe arrived for the reattached stream")
            data = session.last_keyframe
            j = _idr_start(data)
            params = data[: j + 4]
            idr = data[j + 4 :]
            encoder = Encoder("mac", params, 1280, 720, log=self.log)
            encoder.feed(idr)
            session.clear_queue()
        else:
            encoder = Encoder("mac", session.params, 1280, 720, log=self.log)
            encoder.feed(session.first_idr or b"")
        self.encoder = encoder
        session.set_target(encoder)
        self.connected = True
        self.selected_camera = session.camera
        self.selected_inst = "mac"
        self.log("Mac media: encoder started")

    def stop(self) -> None:
        """Stop the browser stream but keep the broker/session alive."""
        self.stop_encoder()

    def close(self) -> None:
        self.stop_encoder()
        if self.session is not None:
            try:
                self.session.stop(force_stop=True)
            except Exception:
                pass
            self.session = None
        self.connected = False
        self.selected_camera = None

    # -- select ------------------------------------------------------------
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

            # Same camera, broker/receiver still alive: reattach on the next keyframe.
            if (
                self.session is not None
                and self.session.camera == name
                and self.session.receiver is not None
                and self.session.seen_first
            ):
                self.stop_encoder()
                self._attach_encoder(self.session, reattach=True)
                return self.status()

            self.stop_encoder()
            cached = self._cache_get(name)
            broker_state = None
            if self.session is not None and self.session.app_alive:
                broker_state = (
                    self.session.camera,
                    self.session.hub,
                    list(self.session.pws),
                    self.session.p100,
                    self.session.session_id,
                )
            if self.warm_switch and cached and self.session is not None and self.session.app_alive:
                try:
                    age = int(time.time() - float(cached.get("saved_at") or 0))
                    self.log(f"trying cached session for {name} (age {age}s)")
                    self.session.start_cached(cached, timeout=12.0)
                    self._cache_put(name, self.session)
                    self._attach_encoder(self.session)
                    self.log("Mac media: cached session live")
                    return self.status()
                except Exception as exc:
                    self.log(f"cached session failed: {exc}")
                    self._cache_drop(name)
                    if broker_state:
                        (
                            self.session.camera,
                            self.session.hub,
                            self.session.pws,
                            self.session.p100,
                            self.session.session_id,
                        ) = broker_state
                        self.session.receiver = None
                        self.log(
                            f"restored broker session {self.session.camera} "
                            f"{self.session.hub}"
                        )

            # Reuse the running app broker to switch cameras (no respawn).
            if self.warm_switch and self.session is not None and self.session.app_alive:
                try:
                    self.log(f"switching camera via broker: {name}")
                    self.session.switch(name, timeout=30.0)
                    self._cache_put(name, self.session)
                    self._attach_encoder(self.session)
                    return self.status()
                except Exception as exc:
                    self.log(f"broker switch failed: {exc}")
                    try:
                        self.session.stop(force_stop=True)
                    except Exception:
                        pass
                    self.session = None

            # Cold start: spawn the app once, then keep it as the broker.
            self.adb.ensure_home()
            session = _Session(self.adb, name, log=self.log)
            try:
                session.start(timeout=timeout)
            except Exception:
                session.stop(force_stop=True)
                raise
            self.session = session
            self._cache_put(name, session)
            self._attach_encoder(session)
            return self.status()

    def status(self) -> dict:
        with self.lock:
            encoder = self.encoder
            st = encoder.stats() if encoder is not None else {}
            session = self.session
            st.update(
                {
                    "mode": "mac",
                    "selected_camera": self.selected_camera,
                    "selected_inst": self.selected_inst,
                    "connected": self.connected or bool(session and session.app_alive),
                    "connected_app": bool(session and session.app_alive),
                    "broker": bool(session and session.app_alive),
                    "instances": (
                        [
                            {
                                "inst": "mac",
                                "short": "mac",
                                "frames": st.get("frames_in", 0),
                                "fps": 0.0,
                                "width": st.get("width", 0),
                                "height": st.get("height", 0),
                                "extra": True,
                                "keyframe": True,
                            }
                        ]
                        if encoder is not None
                        else []
                    ),
                    "session": (
                        {
                            "camera": session.camera,
                            "rtp": session.receiver.rtp,
                            "keyframes": session.receiver.keyframes,
                            "deltas": session.receiver.deltas,
                            "seq_gaps": session.receiver.seq_gaps,
                            "seq_lost": session.receiver.seq_lost,
                            "key": (
                                session.receiver.aes_key.decode("ascii", "replace")
                                if session.receiver.aes_key
                                else None
                            ),
                        }
                        if session and session.receiver
                        else None
                    ),
                }
            )
            return st
