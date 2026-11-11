"""RTP receiver and H.265 access-unit assembly for the direct path.

This module is intentionally independent of the Android app/emulator.  It
receives Link Visual RTP from the camera, validates the ICE checks, decrypts
the 0xbb keyframe fragments, and emits Annex-B H.265 access units to ffmpeg.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from queue import Queue, Empty

import ec4_stun as fit

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
