"""STUN/ICE helpers shared by the direct Mac path.

Bare parsing, verification, binding success, and DATA request construction.
The diagnostic app/ADB runner was moved to the private tree.
"""
import hashlib
import hmac
import socket
import struct
import zlib

MAGIC = b"\x21\x12\xa4\x42"



def parse(d):
    if len(d) < 20 or d[4:8] != MAGIC:
        return None
    typ, ln = struct.unpack(">HH", d[:4])
    attrs = []
    i = 20
    end = min(len(d), 20 + ln)
    while i + 4 <= end:
        t, l = struct.unpack(">HH", d[i:i + 4])
        off = i
        i += 4
        v = d[i:i + l]
        i += l + (l % 4)
        attrs.append((t, l, v, off))
    return typ, attrs


def verify(d, pw):
    r = parse(d)
    if not r:
        return False
    _, attrs = r
    mi = next(((v, o) for t, l, v, o in attrs if t == 8), None)
    if not mi:
        return False
    v, off = mi
    msg = bytearray(d[:off])
    msg[2:4] = struct.pack(">H", off + 24 - 20)
    return hmac.new(pw.encode(), bytes(msg), hashlib.sha1).digest() == v


def build_data_request(template, payload, seq_num, pw):
    """Rebuild the app's startStreaming STUN request with a new DATA payload."""
    r = parse(template)
    if not r:
        return None
    _, attrs = r
    body = bytearray()
    for t, l, v, off in attrs:
        if t in (0x0008, 0x8028, 0x8022):
            continue
        body += struct.pack(">HH", t, l) + v + b"\x00" * (l % 4)
    data = payload.encode() if isinstance(payload, str) else payload
    body += struct.pack(">HH", 0x8022, len(data)) + data + b"\x00" * (len(data) % 4)

    # MESSAGE-INTEGRITY
    hdr = struct.pack(">HH", 0x0001, 0) + MAGIC + template[8:20]
    mi_offset = 20 + len(body)
    hdr = struct.pack(">HH", 0x0001, mi_offset + 24 - 20) + MAGIC + template[8:20]
    mi = hmac.new(pw.encode(), hdr + body, hashlib.sha1).digest()
    body += struct.pack(">HH", 0x0008, 20) + mi

    # FINGERPRINT
    full_len = 20 + len(body) + 8
    hdr = struct.pack(">HH", 0x0001, full_len - 20) + MAGIC + template[8:20]
    crc = (zlib.crc32(hdr + body) & 0xFFFFFFFF) ^ 0x5354554E
    body += struct.pack(">HHI", 0x8028, 4, crc)
    return hdr + body


def success(txid, ip, port, pw):
    xport = port ^ 0x2112
    xaddr = int.from_bytes(socket.inet_aton(ip), "big") ^ 0x2112A442
    attrs = struct.pack(">HH", 0x20, 8) + b"\x00\x01" + struct.pack(">H", xport) + struct.pack(">I", xaddr)
    hdr = struct.pack(">HH", 0x101, len(attrs) + 24) + MAGIC + txid
    mi = hmac.new(pw.encode(), hdr + attrs, hashlib.sha1).digest()
    msg = hdr + attrs + struct.pack(">HH", 8, 20) + mi
    msg = msg[:2] + struct.pack(">H", len(msg) + 8 - 20) + msg[4:]
    crc = (zlib.crc32(msg) & 0xFFFFFFFF) ^ 0x5354554E
    return msg + struct.pack(">HHI", 0x8028, 4, crc)
