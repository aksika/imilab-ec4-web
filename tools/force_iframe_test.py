"""Test a Mac-originated `realplay:forceIFrame` request over the Link Visual
P2P data channel (STUN 0x8022 DATA) and watch the RTP for an IDR.

This is the keyframe half of the hybrid path: the app bootstraps the session,
then the Mac asks the camera for a fresh I-frame so the direct RTP feed can be
decoded from the first frame.
"""
import hashlib
import hmac
import os
import re
import socket
import struct
import time
import zlib

LOG = "/tmp/ec4_live_iframe.log"
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


def main():
    p100 = p256 = None
    pws = []
    deadline = time.time() + 75
    with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
        while time.time() < deadline:
            line = fh.readline()
            if not line:
                time.sleep(0.2)
                continue
            m = re.search(r"MI pwd=([\d,]+)", line)
            if m:
                try:
                    pw = bytes(int(x) for x in m.group(1).split(",") if x).decode("ascii", "replace")
                    if len(pw) >= 20 and pw not in pws:
                        pws.append(pw)
                        print("PW", repr(pw), flush=True)
                except Exception:
                    pass
            m = re.search(r"\[NET\] sendto dest=2:([\d.]+):(\d+) len=(\d+) hex=([0-9a-f]+)", line)
            if m:
                ip, port, ln, hx = m.groups()
                ln = int(ln)
                d = bytes.fromhex(hx)
                if ln == 100 and len(d) == 100:
                    p100 = (ip, int(port), d)
                if ln == 256 and len(d) == 256:
                    p256 = (ip, int(port), d)
            if p100 and p256 and len(pws) >= 2:
                break
    print("got", bool(p100), bool(p256), pws, flush=True)
    if not p100 or not p256 or len(pws) < 2:
        return
    hub = (p100[0], p100[1])
    remote = next((p for p in pws if verify(p256[2], p)), None)
    if not remote:
        print("no remote password", flush=True)
        return
    print("hub", hub, "remote", repr(remote), flush=True)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.4)
    s.connect(hub)
    lip, lport = s.getsockname()
    print("local", lip, lport, flush=True)
    s.send(p100[2])

    # Immediately stop the app's checks so the hub selects us.
    import subprocess
    ADB = os.environ.get("EC4_ADB") or "adb"
    subprocess.run([ADB, "shell", "su", "0", "iptables", "-I", "OUTPUT", "-p", "udp",
                    "--dport", str(hub[1]), "-j", "DROP"], capture_output=True, text=True)

    # Continue the app's request sequence from our socket.
    template_attrs = parse(p256[2])[1]
    original_json = next(v for t, l, v, o in template_attrs if t == 0x8022)
    print("request JSON", original_json, flush=True)
    json3 = original_json.replace(b'"seq_num":2', b'"seq_num":3')
    seq_request = build_data_request(p256[2], json3, "3", remote)
    print("seq3", bool(seq_request), flush=True)

    start = time.time()
    rtp = 0
    iframe_sent = 0
    nal_types = {}
    check_local = None
    keyframe_at = None
    while time.time() - start < 40:
        try:
            d = s.recv(65535)
        except socket.timeout:
            d = None
        if d:
            r = parse(d)
            if r:
                typ, attrs = r
                if typ == 1:
                    if check_local is None:
                        check_local = next((p for p in pws if verify(d, p)), None)
                        print("local_pw", check_local, flush=True)
                    if check_local:
                        s.send(success(d[8:20], lip, lport, check_local))
            elif len(d) >= 12 and (d[0] >> 6) == 2:
                rtp += 1
                pl = d[12:]
                if pl:
                    h = pl[0]
                    if h == 0x0a and len(pl) > 6:
                        nal_type = (pl[6] >> 1) & 0x3F
                        nal_types[nal_type] = nal_types.get(nal_type, 0) + 1
                        if nal_type in (19, 20, 32, 33, 34) and keyframe_at is None:
                            keyframe_at = rtp
                            print("KEYFRAME nal type", nal_type, "at rtp", rtp, flush=True)
                if rtp == 1:
                    print("first RTP, sending bare forceIFrame", flush=True)
                    bare = build_data_request(p256[2], "realplay:forceIFrame", "4", remote)
                    if bare:
                        s.send(bare)
                        iframe_sent += 1
                if rtp == 40 and iframe_sent == 1:
                    print("sending JSON forceIFrame", flush=True)
                    js = ('{"version":"2.0","seq_num":5,"type":"request","params":{"messageType":"realplay:forceIFrame"}}')
                    req = build_data_request(p256[2], js, "5", remote)
                    if req:
                        s.send(req)
                        iframe_sent += 1
                if rtp == 120 and iframe_sent == 2:
                    print("sending JSON realplay:forceIFrame (with streamType)", flush=True)
                    js = ('{"version":"2.0","seq_num":6,"type":"request","params":{"messageType":"realplay:forceIFrame","streamType":0}}')
                    req = build_data_request(p256[2], js, "6", remote)
                    if req:
                        s.send(req)
                        iframe_sent += 1
        if rtp > 900:
            break
        if time.time() - start > 4 and rtp == 0 and seq_request:
            s.send(seq_request)
            seq_request = None
    print("RESULT rtp", rtp, "iframe_sent", iframe_sent, "keyframe_at", keyframe_at, "nal_types", nal_types, flush=True)
    subprocess.run([ADB, "shell", "su", "0", "iptables", "-D", "OUTPUT", "-p", "udp",
                    "--dport", str(hub[1]), "-j", "DROP"], capture_output=True, text=True)


if __name__ == "__main__":
    main()
