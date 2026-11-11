"""App-free Link Visual P2P media path (Mac side).

Flow:
  1. Wake the camera (`SleepStatus=1`) and wait for `ActiveStatus=1`.
  2. Query `/vision/customer/stream/query`; register with the STUN server (the
     MI key is the query `key`), and optionally invoke `StartP2PStreaming`.
  3. Connect to `p2pInfo.signalUrl`; send our own SDP offer + host candidate
     **and a srflx candidate from public STUN** (the app always advertises
     both).  Resend the candidates after the answer: the camera ignores
     candidates sent before its ICE session exists.
  4. Send STUN binding requests to the camera's LAN candidate using the app's
     exact ICE attributes (ICE-CONTROLLED, 0xC057 network id/cost,
     priority 0x6E7F1EFF, USE-CANDIDATE), answer the camera's checks, and send
     `startStreaming` once ICE is up.
  5. Capture RTP, decrypt/reassemble the first H.265 keyframe.

Known limitation (2026-11-09): the camera's ICE agent only answers while the
app's native player session has been active recently (roughly a 2-3 minute
warm window).  Cold starts return `answerSdp` but no binding checks.  The
cloud-side opener is still unknown; see PROTOCOL.md.

Usage:
    .venv/bin/python tools/p2p_media.py [seconds]
Environment:
    EC4_START_MODE=push|base|full|stunbase|signalbase|after
    EC4_WARMUP=seconds        wait after the camera becomes active
    EC4_RELAY=1               keep an RTMP relay viewer connected (trigger)
    EC4_STOP_PUSH=seconds     stop the camera's relay push after N seconds
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iot_auth  # noqa: E402
import p2p_signal  # noqa: E402
import ec4_config  # noqa: E402
from ec4_stun import parse, verify, success  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

IOT_ID = os.environ.get("EC4_IOT_ID") or ec4_config.first_camera_id()
LAN_IP = ec4_config.lan_ip()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RTP_PATH = os.path.join(ROOT, "captures", "appfree_rtp.bin")
KF_PATH = os.path.join(ROOT, "captures", "appfree_keyframe.h265")
MAGIC = b"\x21\x12\xa4\x42"
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 25


def attr(t, v):
    return struct.pack(">HH", t, len(v)) + v + b"\x00" * ((4 - len(v) % 4) % 4)


def stun_message(txid, body, pw):
    hdr = struct.pack(">HH", 0x0001, len(body) + 24) + MAGIC + txid
    body += attr(0x0008, hmac.new(pw.encode(), hdr + body, hashlib.sha1).digest())
    hdr = struct.pack(">HH", 0x0001, len(body) + 8) + MAGIC + txid
    crc = (zlib.crc32(hdr + body) & 0xFFFFFFFF) ^ 0x5354554E
    body += struct.pack(">HHI", 0x8028, 4, crc)
    return hdr + body


def binding_request(ufrag_local, ufrag_remote, pw, tiebreaker, use_candidate=True):
    txid = os.urandom(12)
    body = attr(0x0006, (ufrag_remote + ":" + ufrag_local).encode())
    body += attr(0xC057, b"\x00\x01\x00\x32")  # network-id 1 / network-cost 50
    body += attr(0x802A, struct.pack(">Q", tiebreaker))  # ICE-CONTROLLED
    if use_candidate:
        body += attr(0x0025, b"")
    body += attr(0x0024, struct.pack(">I", 0x6E7F1EFF))
    return stun_message(txid, body, pw)


def data_request(ufrag_local, ufrag_remote, pw, payload, tiebreaker):
    txid = os.urandom(12)
    body = attr(0x0006, (ufrag_remote + ":" + ufrag_local).encode())
    body += attr(0xC057, b"\x00\x01\x00\x32")
    body += attr(0x802A, struct.pack(">Q", tiebreaker))
    body += attr(0x0025, b"")
    body += attr(0x0024, struct.pack(">I", 0x6E7F1EFF))
    body += attr(0x8022, payload.encode() if isinstance(payload, str) else payload)
    return stun_message(txid, body, pw)


def pick_host(cands):
    for c in cands:
        m = re.search(r"udp \d+ ([\d.]+) (\d+) typ host", c)
        if m and m.group(1).startswith("192.168."):
            return m.group(1), int(m.group(2))
    return None


def aes_cbc_decrypt(key, data):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    dec = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()
    return dec.update(data) + dec.finalize()


def reassemble(fragments, key):
    frame = bytearray()
    first = True
    params = b""
    for seq, payload in sorted(fragments):
        if not payload:
            continue
        if payload[0] == 0x09:
            data, tail = payload[2:], b""
        elif payload[0] == 0xBB:
            body = payload[4:]
            aes_len = (len(body) // 16) * 16
            plain = aes_cbc_decrypt(key, body[:aes_len])
            data, tail = plain[2:], body[aes_len:]
            if first:
                j = data.find(b"\x00\x00\x00\x01")
                if j >= 0:
                    k = data.find(b"\x00\x00\x00\x01", j + 4)
                    while k >= 0:
                        nal_type = (data[k + 4] >> 1) & 0x3F
                        if nal_type in (19, 20):
                            break
                        k = data.find(b"\x00\x00\x00\x01", k + 4)
                    if k >= 0:
                        params = data[j:k]
                        data = data[k + 4:]
        else:
            continue
        first = False
        frame += data
        frame += tail
    return params, bytes(frame)


def analyze_rtp(path, key):
    data = open(path, "rb").read()
    i = 0
    recs = []
    while i + 12 <= len(data):
        ts, ln = struct.unpack(">dI", data[i:i + 12])
        i += 12
        recs.append((ts, data[i:i + ln]))
        i += ln
    rtp = [(ts, b) for ts, b in recs if len(b) >= 12 and (b[0] >> 6) == 2]
    bursts = defaultdict(list)
    for ts, b in rtp:
        pl = b[12:]
        if pl and pl[0] in (0x09, 0xBB):
            ssrc = int.from_bytes(b[8:12], "big")
            rtpts = int.from_bytes(b[4:8], "big")
            bursts[(ssrc, rtpts)].append((int.from_bytes(b[2:4], "big"), pl))
    print("rtp", len(rtp), "keyframe bursts", len(bursts), flush=True)
    if not bursts:
        return
    saved = 0
    for i, ((ssrc, rtpts), frags) in enumerate(
            sorted(bursts.items(), key=lambda kv: kv[1][0][0])):
        params, idr = reassemble(frags, key)
        ntype = (idr[0] >> 1) & 0x3F if idr else None
        print("burst", i, "frags", len(frags), "params", len(params),
              "idr", len(idr), "type", ntype, flush=True)
        if i == 0 and idr:
            with open(KF_PATH, "wb") as fh:
                fh.write(params + b"\x00\x00\x00\x01" + idr)
            saved = len(params) + 4 + len(idr)
    print("saved", KF_PATH, saved, flush=True)


def udp_worker(udp, host, ufrag, pwd, cu, cp, sess, stop, tiebreaker):
    out = None
    rtp = checks = 0
    success_seen = start_sent = False
    start = time.time()
    last_bind = start - 100
    last_progress = start
    # The camera's ICE agent is opened by the startStreaming DATA indication;
    # send it immediately after the first binding request, as the app does.
    try:
        udp.sendto(binding_request(ufrag, cu, cp, tiebreaker), host)
        print("sent binding request", flush=True)
    except Exception as exc:
        print("bind send err", type(exc).__name__, exc, flush=True)
    time.sleep(0.1)
    payload = ('{"version":"2.0","seq_num":2,"type":"request","params":'
               '{"messageType":"startStreaming","streamType":0,"sessionId":"%s"}}' % sess)
    try:
        udp.sendto(data_request(ufrag, cu, cp, payload, tiebreaker), host)
        start_sent = True
        print("sent startStreaming (immediate)", flush=True)
    except Exception as exc:
        print("start send err", type(exc).__name__, exc, flush=True)
    while not stop.is_set() and time.time() - start < DURATION:
        now = time.time()
        if not start_sent and now - last_bind > 1.0:
            try:
                udp.sendto(binding_request(ufrag, cu, cp, tiebreaker), host)
                print("sent binding request", flush=True)
            except Exception as exc:
                print("bind send err", type(exc).__name__, exc, flush=True)
            last_bind = now
        if not start_sent and (success_seen or checks) and time.time() - start > 0.5:
            payload = ('{"version":"2.0","seq_num":2,"type":"request","params":'
                       '{"messageType":"startStreaming","streamType":0,"sessionId":"%s"}}' % sess)
            try:
                udp.sendto(data_request(ufrag, cu, cp, payload, tiebreaker), host)
                start_sent = True
                print("sent startStreaming", flush=True)
            except Exception as exc:
                print("start send err", type(exc).__name__, exc, flush=True)
        try:
            d, addr = udp.recvfrom(65535)
        except socket.timeout:
            continue
        except Exception as exc:
            print("recv err", type(exc).__name__, exc, flush=True)
            continue
        r = parse(d)
        if r:
            typ, _ = r
            if typ == 1 and verify(d, pwd):
                udp.sendto(success(d[8:20], addr[0], addr[1], pwd), addr)
                checks += 1
                print("camera check", checks, flush=True)
            elif typ in (0x0101, 0x0111):
                success_seen = True
                print("binding success", flush=True)
            continue
        if len(d) >= 12 and (d[0] >> 6) == 2:
            if out is None:
                out = open(RTP_PATH, "wb")
            rtp += 1
            if time.time() - last_progress >= 5:
                print("progress rtp=%d checks=%d t=%.1f" % (rtp, checks, time.time() - start), flush=True)
                last_progress = time.time()
            out.write(struct.pack(">dI", time.time(), len(d)) + d)
            if rtp in (1, 2, 3):
                print("RTP", rtp, len(d), d[12:20].hex(), flush=True)
    if out:
        out.close()
    print("RESULT rtp=%d checks=%d success=%s start=%s" % (
        rtp, checks, success_seen, start_sent), flush=True)
    return cp


def public_stun(udp, timeout=2.0):
    """Return (ip, port) as seen by a public STUN server, using `udp`."""
    try:
        txid = os.urandom(12)
        pkt = struct.pack(">HH", 0x0001, 0) + MAGIC + txid
        udp.sendto(pkt, ("stun.l.google.com", 19302))
        old = udp.gettimeout()
        udp.settimeout(timeout)
        try:
            while True:
                d, _ = udp.recvfrom(2048)
                if len(d) < 20 or d[4:8] != MAGIC or d[8:20] != txid:
                    continue
                i = 20
                end = min(len(d), 20 + struct.unpack(">H", d[2:4])[0])
                while i + 4 <= end:
                    t, l = struct.unpack(">HH", d[i:i + 4])
                    i += 4
                    v = d[i:i + l]
                    i += l + ((4 - l % 4) % 4)
                    if t == 0x0020 and l >= 8:
                        port = struct.unpack(">H", v[2:4])[0] ^ 0x2112
                        ip = bytes(a ^ b for a, b in zip(v[4:8], MAGIC[:4]))
                        return ".".join(str(x) for x in ip), port
                    if t == 0x0001 and l >= 8:
                        port = struct.unpack(">H", v[2:4])[0]
                        return ".".join(str(x) for x in v[4:8]), port
        finally:
            udp.settimeout(old)
    except Exception as exc:
        print("public stun err", type(exc).__name__, exc, flush=True)
    return None


def stun_register(stun_url, sess, timeout=3.0):
    """Register this session with the Link Visual STUN rendezvous.

    Username `client-<session>`, MESSAGE-INTEGRITY keyed with the stream
    query's `key` parameter.
    """
    host, port = stun_url.split("?")[0].split(":")
    q = urllib.parse.parse_qs(urllib.parse.urlparse("stun://" + stun_url).query)
    key = (q.get("key") or [""])[0]
    if not key:
        return False, b""
    txid = os.urandom(12)
    body = attr(0x0006, ("client-" + sess).encode())
    hdr = struct.pack(">HH", 0x0001, len(body) + 24) + MAGIC + txid
    body += attr(0x0008, hmac.new(key.encode(), hdr + body, hashlib.sha1).digest())
    hdr = struct.pack(">HH", 0x0001, len(body) + 8) + MAGIC + txid
    pkt = hdr + body
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((LAN_IP, 0))
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (host, int(port)))
        d, addr = s.recvfrom(2048)
        return True, d
    except Exception as exc:
        print("stun register err", type(exc).__name__, exc, flush=True)
        return False, b""
    finally:
        s.close()


def camera_active(iot, iot_id=None):
    res = iot_auth.thing_properties_get(iot_id or IOT_ID, iot)
    try:
        data = json.loads(res["body"])["data"]
        return (data.get("ActiveStatus") or {}).get("value")
    except Exception:
        return None


async def main():
    creds = iot_auth.load_creds()
    iot = creds.get("iotToken")
    if os.environ.get("EC4_NO_WAKE") == "1":
        print("skip wake (relay trigger)", flush=True)
    else:
        print("wake", iot_auth.thing_properties_set(IOT_ID, iot, {"SleepStatus": 1})["status"], flush=True)
        # Battery cameras need time to come out of deep sleep and bring up WiFi/P2P.
        active = None
        for _ in range(25):
            await asyncio.sleep(1.5)
            active = camera_active(iot)
            if active == 1:
                break
        print("ActiveStatus", active, flush=True)
        warmup = int(os.environ.get("EC4_WARMUP", "12"))
        if warmup:
            await asyncio.sleep(warmup)
    extra = None
    if os.environ.get("EC4_IMI_HEADERS"):
        try:
            import imi_auth
            t = imi_auth.load_tokens()
            extra = {
                "X-IMILAB-TOKEN": t.get("token") or "",
                "X-IMILAB-IDENTITYID": t.get("identityId") or "",
                "Authorization": t.get("token") or "",
                "APP-ID": imi_auth.APP_ID,
                "IMI-APP-ID": imi_auth.IMI_APP_ID,
                "APP-VERSION": "2.9.2",
                "APP-VERSION-CODE": "128",
                "Client-OS": "android",
                "X-Multi-Language": "en",
                "X-Ali-Language": "en-US",
            }
            print("imi headers on", flush=True)
        except Exception as exc:
            print("imi headers err", exc, flush=True)
    res = iot_auth.stream_query(IOT_ID, iot, extra_headers=extra)
    data = json.loads(res["body"])["data"]
    p2p = data["p2pInfo"]
    url = p2p["signalUrl"]
    stun = p2p["stunUrl"]
    sess = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["session"][0]
    print("session", sess, flush=True)
    ok, stun_resp = stun_register(stun, sess)
    print("stun register", ok, len(stun_resp), stun_resp.hex()[:120], flush=True)

    # The camera's P2P agent may need the server endpoints (not the client
    # session URL).  Test several formats via EC4_START_MODE.
    mode = os.environ.get("EC4_START_MODE", "push")
    base_signal = "wss://iotx-vision-p2p-signal-eu-central-1.aliyuncs.com:443/iotx/vision/p2p/request"
    base_stun = stun.split("?")[0]
    if mode == "base":
        start_stun, start_signal = base_stun, base_signal
    elif mode == "stunbase":
        start_stun, start_signal = base_stun, url
    elif mode == "signalbase":
        start_stun, start_signal = stun, base_signal
    else:
        start_stun, start_signal = stun, url
    viewer = None
    if os.environ.get("EC4_RELAY"):
        relay = data.get("relayUrl") or ""
        if relay:
            rest = relay.split("://", 1)[1]
            hostport, path = rest.split("/", 1)
            if "/" in path:
                app, play = path.rsplit("/", 1)
                if os.environ.get("EC4_RELAY_PRIVATE") == "1":
                    env = dict(os.environ)
                    env["EC4_RELAY_URL"] = relay
                    cmd = [sys.executable, os.path.join(ROOT, "tools", "rtmp_private_viewer.py"),
                           str(DURATION + 30)]
                    print("relay private viewer", flush=True)
                    viewer = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, env=env)
                elif os.environ.get("EC4_RELAY_KEEP") == "1":
                    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                           "-rtmp_live", "live", "-rtmp_app", app,
                           "-i", "rtmp://%s/%s" % (hostport, play),
                           "-t", str(DURATION + 30), "-f", "null", "-"]
                else:
                    cmd = ["ffprobe", "-v", "error", "-rtmp_app", app,
                           "-rtmp_live", "live", "-timeout", "30000000",
                           "rtmp://%s/%s" % (hostport, play)]
            else:
                cmd = ["ffprobe", "-v", "error", "-rtmp_live", "live",
                       "-timeout", "5000000", relay]
            if viewer is None:
                print("relay starter", " ".join(cmd)[:220], flush=True)
                # Keep one viewer connected for the whole run: this is what
                # makes the camera start pushing (and open its P2P agent).
                viewer = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
            await asyncio.sleep(int(os.environ.get("EC4_RELAY_WAIT", "0")))
            if os.environ.get("EC4_RELAY_KEEP") == "1" or os.environ.get("EC4_RELAY_PRIVATE") == "1":
                active = None
                for _ in range(30):
                    active = camera_active(iot)
                    if active == 1:
                        break
                    await asyncio.sleep(1.0)
                print("relay ActiveStatus", active, flush=True)
                await asyncio.sleep(int(os.environ.get("EC4_RELAY_WARMUP", "3")))
    if mode == "push":
        key = (data.get("relayDecryptKey") or {}).get("key") or ""
        starter = iot_auth.start_push_streaming(
            IOT_ID, iot, data.get("relayUrl") or "", 0, 0, key, 0, 0)
        print("StartPushStreaming", starter["status"], starter["body"][:160], flush=True)
        await asyncio.sleep(int(os.environ.get("EC4_START_WAIT", "4")))
    elif mode != "after":
        starter = iot_auth.start_p2p_streaming(IOT_ID, iot, start_stun, start_signal)
        print("StartP2PStreaming", mode, starter["status"], starter["body"][:160], flush=True)
        await asyncio.sleep(int(os.environ.get("EC4_START_WAIT", "4")))

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((LAN_IP, 0))
    udp.settimeout(0.2)
    lport = udp.getsockname()[1]
    pub = public_stun(udp)
    print("public stun", pub, flush=True)
    ufrag, pwd = p2p_signal._ufrag(5), p2p_signal._pwd()
    tiebreaker = int.from_bytes(os.urandom(8), "big")
    print("local %s:%d ufrag=%s tie=%x" % (LAN_IP, lport, ufrag, tiebreaker), flush=True)
    offer = p2p_signal._msg({
        "iotId": "", "clientId": sess, "messageType": "offerSdp",
        "clientType": "pc", "channelType": "websocket", "Version": "1.3.2",
        "sessionId": sess, "sdp": p2p_signal._offer_sdp(ufrag, pwd),
    })
    cand = p2p_signal._msg({
        "sdpMid": "video", "sdpMLineIndex": 0,
        "candidate": p2p_signal._candidate(LAN_IP, lport, ufrag),
        "iotId": "", "clientId": sess, "messageType": "candidate",
        "clientType": "pc", "channelType": "websocket", "sessionId": sess,
    })
    cand_list = [cand]
    if pub:
        mapped = "%s:%d" % pub
        srflx = ("candidate:%d 1 udp 1686052607 %s %d typ srflx raddr %s rport %d "
                 "generation 0 ufrag %s network-id 1 network-cost 50") % (
            int.from_bytes(os.urandom(4), "big"), pub[0], pub[1], LAN_IP, lport, ufrag)
        cand_list.append(p2p_signal._msg({
            "sdpMid": "video", "sdpMLineIndex": 0, "candidate": srflx,
            "iotId": "", "clientId": sess, "messageType": "candidate",
            "clientType": "pc", "channelType": "websocket", "sessionId": sess,
            "priority": 1, "simpleNatType": 0,
            "mappedAddressList": [mapped, mapped, mapped],
        }))
        print("srflx", srflx, flush=True)

    answer = None
    cands = []
    async with connect(url, max_size=2**21, additional_headers={"Expect": "101"}) as ws:
        await ws.send(offer)
        for c in cand_list:
            await ws.send(c)
        if mode == "after":
            starter = iot_auth.start_p2p_streaming(IOT_ID, iot, start_stun, start_signal)
            print("StartP2PStreaming(after)", starter["status"], starter["body"][:160], flush=True)
        t0 = time.time()
        while time.time() - t0 < 4:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            p = json.loads(msg).get("params") or {}
            if p.get("messageType") == "answerSdp":
                answer = p.get("sdp")
            elif p.get("messageType") == "candidate":
                cands.append(p.get("candidate"))
            if answer and pick_host(cands):
                break
        if not answer:
            print("NO ANSWER", flush=True)
            return
        # Critical: reset our candidate after the camera's answer.  The camera
        # ignores candidates sent before its ICE session exists.
        for c in cand_list:
            await ws.send(c)
        await asyncio.sleep(0.2)
        cu = re.search(r"a=ice-ufrag:(\S+)", answer).group(1)
        cp = re.search(r"a=ice-pwd:(\S+)", answer).group(1)
        host = pick_host(cands)
        print("camera ufrag=%s host=%s" % (cu, host), flush=True)
        if not host:
            print("no host candidate", flush=True)
            return
        print("media AES key", cp[:16].encode(), flush=True)
        stop = threading.Event()
        th = threading.Thread(target=udp_worker, args=(
            udp, host, ufrag, pwd, cu, cp, sess, stop, tiebreaker), daemon=True)
        th.start()
        stop_after = int(os.environ.get("EC4_STOP_PUSH", "0"))
        if stop_after:
            def stop_push():
                time.sleep(stop_after)
                for ident, args in (("StopPushStreaming", {"StreamType": 0}),
                                    ("StopMediaStreamPush", {})):
                    r = iot_auth.invoke_service(IOT_ID, iot, ident, args)
                    print("stop", ident, r["status"], r["body"][:120], flush=True)
            threading.Thread(target=stop_push, daemon=True).start()
        await asyncio.sleep(DURATION)
        stop.set()
        th.join(timeout=2)
        if viewer:
            try:
                viewer.terminate()
                viewer.wait(timeout=3)
            except Exception:
                try:
                    viewer.kill()
                except Exception:
                    pass
        analyze_rtp(RTP_PATH, cp[:16].encode())


if __name__ == "__main__":
    asyncio.run(main())
