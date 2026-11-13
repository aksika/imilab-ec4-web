#!/usr/bin/env python3
"""Minimal RTMP relay viewer using the app's private version-4 handshake.

The Imilab player sends RTMP C0=0x04 (not 0x03) and then a standard C1.  The
relay accepts that and the app's private app name `glive?token=...`.  This
viewer keeps the relay session open, which is what wakes the battery camera
and can open its P2P agent.

Usage:
    .venv/bin/python tools/rtmp_private_viewer.py [seconds]
"""
from __future__ import annotations

import json
import os
import random
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iot_auth  # noqa: E402
import ec4_config  # noqa: E402

IOT_ID = os.environ.get("EC4_IOT_ID") or ec4_config.first_camera_id()


def amf_str(s: str) -> bytes:
    b = s.encode()
    return b"\x02" + struct.pack(">H", len(b)) + b


def amf_num(n: float) -> bytes:
    return b"\x00" + struct.pack(">d", n)


def amf_bool(v: bool) -> bytes:
    return b"\x01" + (b"\x01" if v else b"\x00")


def amf_null() -> bytes:
    return b"\x05"


def amf_obj(d: dict) -> bytes:
    out = bytearray(b"\x03")
    for k, v in d.items():
        kb = k.encode()
        out += struct.pack(">H", len(kb)) + kb
        if isinstance(v, bool):
            out += amf_bool(v)
        elif isinstance(v, (int, float)):
            out += amf_num(float(v))
        else:
            out += amf_str(str(v))
    out += b"\x00\x00\x09"
    return bytes(out)


def send_msg_fmt1(sock: socket.socket, csid: int, typ: int, payload: bytes,
                  chunk_size: int = 4096) -> None:
    """RTMP fmt=1 chunk (inherits the message stream id)."""
    hdr = bytes([0x40 | csid]) + b"\x00\x00\x00" + struct.pack(">I", len(payload))[1:] + bytes([typ])
    sock.sendall(hdr + payload[:chunk_size])
    off = chunk_size
    while off < len(payload):
        part = payload[off:off + chunk_size]
        sock.sendall(bytes([0xC0 | csid]) + part)
        off += len(part)


def send_msg(sock: socket.socket, csid: int, msid: int, typ: int, payload: bytes,
             chunk_size: int = 4096) -> None:
    ts = b"\x00\x00\x00"
    hdr = bytes([csid]) + ts + struct.pack(">I", len(payload))[1:] + bytes([typ]) + struct.pack("<I", msid)
    first = payload[:chunk_size]
    sock.sendall(hdr + first)
    off = len(first)
    while off < len(payload):
        part = payload[off:off + chunk_size]
        sock.sendall(bytes([0xC0 | csid]) + part)
        off += len(part)


def read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise EOFError("relay closed during handshake")
        buf += part
    return buf


def handshake(sock: socket.socket) -> None:
    # The app's private C1 starts with the process millisecond clock, then
    # four zero bytes and 1528 random bytes.  The relay accepts any fresh
    # value; reusing an old one can trip its replay cache.
    try:
        now_ms = int(time.monotonic() * 1000) & 0xFFFFFFFF
    except Exception:
        now_ms = int(time.time() * 1000) & 0xFFFFFFFF
    c1 = struct.pack(">I", now_ms) + b"\x00\x00\x00\x00" + os.urandom(1528)
    sock.sendall(b"\x04" + c1)


def parse_amf_strings(payload: bytes) -> list[str]:
    out = []
    i = 0
    while i + 3 <= len(payload):
        if payload[i] == 0x02:
            n = struct.unpack(">H", payload[i + 1:i + 3])[0]
            if 0 < n < 256 and i + 3 + n <= len(payload):
                s = payload[i + 3:i + 3 + n]
                if all(32 <= c < 127 for c in s):
                    out.append(s.decode("ascii", "replace"))
                    i += 3 + n
                    continue
        i += 1
    return out


def parse_amf_numbers(payload: bytes) -> list[float]:
    out = []
    i = 0
    while i + 9 <= len(payload):
        if payload[i] == 0x00:
            out.append(struct.unpack(">d", payload[i + 1:i + 9])[0])
            i += 9
        else:
            i += 1
    return out


class ChunkReader:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.chunk_size = 128
        self.state: dict[int, dict] = {}

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            part = self.sock.recv(65536)
            if not part:
                raise EOFError("relay closed")
            self.buf += part
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def message(self) -> tuple[int, int, bytes]:
        while True:
            b0 = self._read(1)[0]
            fmt = b0 >> 6
            csid = b0 & 0x3F
            if csid == 0:
                csid = 64 + self._read(1)[0]
            elif csid == 1:
                b = self._read(2)
                csid = 64 + b[0] + b[1] * 256
            st = self.state.setdefault(csid, {"ts": 0, "len": 0, "type": 0,
                                              "msid": 0, "payload": b""})
            if fmt == 0:
                h = self._read(11)
                ts = int.from_bytes(h[0:3], "big")
                ln = int.from_bytes(h[3:6], "big")
                typ = h[6]
                msid = int.from_bytes(h[7:11], "little")
                st.update(ts=ts, len=ln, type=typ, msid=msid, payload=b"")
            elif fmt == 1:
                h = self._read(7)
                st["ts"] += int.from_bytes(h[0:3], "big")
                st["len"] = int.from_bytes(h[3:6], "big")
                st["type"] = h[6]
                st["payload"] = b""
            elif fmt == 2:
                h = self._read(3)
                st["ts"] += int.from_bytes(h[0:3], "big")
                st["payload"] = b""
            # fmt 3 continues the current message
            need = st["len"] - len(st["payload"])
            take = min(need, self.chunk_size)
            st["payload"] += self._read(take)
            if len(st["payload"]) >= st["len"]:
                payload = st["payload"]
                typ = st["type"]
                st["payload"] = b""
                if typ == 1 and len(payload) >= 4:
                    self.chunk_size = int.from_bytes(payload[:4], "big") & 0x7FFFFFFF
                return csid, typ, payload


def main() -> None:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    parent_pid = int(os.environ.get("EC4_PARENT_PID") or "0")
    if parent_pid and os.getppid() != parent_pid:
        print("parent exited, closing", flush=True)
        return
    creds = iot_auth.load_creds()
    if creds.get("refreshToken") and creds.get("identityId"):
        r = iot_auth.refresh(creds["identityId"], creds["refreshToken"])
        if r["status"] == 200:
            creds.update(json.loads(r["body"]).get("data") or {})
            iot_auth.save_creds(creds)
    iot = creds.get("iotToken")
    relay = os.environ.get("EC4_RELAY_URL")
    if not relay:
        q = iot_auth.stream_query(IOT_ID, iot)
        data = json.loads(q["body"])["data"]
        relay = data["relayUrl"]
    rest = relay.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    app0, play = path.rsplit("/", 1)
    query = app0.split("?", 1)[1]
    app = "glive?" + query
    tc_url = "rtmp://%s/%s" % (hostport, app0)
    playpath = play
    host, port = hostport.rsplit(":", 1)
    print("relay", host, port, "app", app.split("?")[0], "play", playpath[:18], flush=True)

    sock = socket.create_connection((host, int(port)), timeout=15)
    sock.settimeout(15)
    handshake(sock)

    connect = amf_str("connect") + amf_num(1) + amf_obj({
        "app": app, "tcUrl": tc_url, "fpad": False, "capabilities": 15,
        "audioCodecs": 4071, "videoCodecs": 252, "videoFunction": 1,
    })
    send_msg(sock, 3, 0, 20, connect)
    # Private handshake: S0+S1 follows the connect; then the RTMP _result.
    s0 = read_exact(sock, 1)
    read_exact(sock, 1536)
    print("handshake version", s0[0], "ok", flush=True)
    sock.settimeout(0.5)
    try:
        early = sock.recv(4096)
        print("early bytes", len(early), "result", b"_result" in early, flush=True)
    except Exception:
        pass
    sock.settimeout(15)

    play_cmd = (amf_str("play") + amf_num(2) + amf_null() +
                amf_str(playpath) + amf_num(-1000.0))
    send_msg(sock, 8, 1, 20, play_cmd)
    print("play sent", flush=True)

    audio_sent = os.environ.get("EC4_RELAY_PING", "1") != "1"
    if os.environ.get("EC4_RELAY_PING_EARLY") == "1":
        audio = amf_str("onStatus") + amf_num(3) + amf_null() + amf_obj({
            "level": "status", "code": "NetStream.RequestAudioType",
            "description": "desc", "audio_type": "1",
        })
        send_msg_fmt1(sock, 3, 20, audio)
        ping = amf_str("onStatus") + amf_num(4) + amf_null() + amf_obj({
            "level": "status", "code": "NetStream.Ping.Start",
            "description": "desc",
        })
        send_msg_fmt1(sock, 3, 20, ping)
        audio_sent = True
        print("audio/ping sent early", flush=True)
    start = time.time()
    sock.settimeout(1.0)
    total = 0
    while time.time() - start < seconds:
        if parent_pid and os.getppid() != parent_pid:
            print("parent exited, closing", flush=True)
            break
        try:
            data = sock.recv(65536)
            if not data:
                print("relay closed", flush=True)
                break
            total += len(data)
            try:
                names = parse_amf_strings(data)
                if names:
                    print("recv", names[:6], flush=True)
            except Exception:
                pass
            if not audio_sent and b"NetStream.Play.Start" in data:
                audio = amf_str("onStatus") + amf_num(3) + amf_null() + amf_obj({
                    "level": "status", "code": "NetStream.RequestAudioType",
                    "description": "desc", "audio_type": "1",
                })
                send_msg_fmt1(sock, 3, 20, audio)
                ping = amf_str("onStatus") + amf_num(4) + amf_null() + amf_obj({
                    "level": "status", "code": "NetStream.Ping.Start",
                    "description": "desc",
                })
                send_msg_fmt1(sock, 3, 20, ping)
                audio_sent = True
                print("audio/ping sent after Play.Start", flush=True)
        except socket.timeout:
            continue
        except ConnectionResetError:
            print("relay reset", flush=True)
            break
    print("viewer bytes", total, flush=True)
    sock.close()


if __name__ == "__main__":
    main()
