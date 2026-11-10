"""App-free Link Visual P2P signaling.

Queries a fresh stream session from the Alibaba IoT API, connects to the
signal WebSocket as the app does, sends an SDP offer plus a host candidate,
and prints the answer/candidates returned by the camera.

This is the first half of the app-free bootstrap; ICE/media handling lives in
web/ec4_mac.py.

Usage:
    .venv/bin/python tools/p2p_signal.py probe [iotId]
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import string
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iot_auth  # noqa: E402
import ec4_config  # noqa: E402

IOT_ID = os.environ.get("EC4_IOT_ID") or ec4_config.first_camera_id()
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "captures", "signal_probe.jsonl")
ICE_CHARS = string.ascii_letters + string.digits + "+/"


def _ufrag(n: int = 4) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def _pwd(n: int = 24) -> str:
    return "".join(secrets.choice(ICE_CHARS) for _ in range(n))


def _candidate(ip: str, port: int, ufrag: str, priority: int = 2122260223) -> str:
    return (f"candidate:{secrets.randbelow(2**31)} 1 udp {priority} {ip} {port} "
            f"typ host generation 0 ufrag {ufrag} network-id 1 network-cost 50")


def _offer_sdp(ufrag: str, pwd: str) -> str:
    cname = "".join(secrets.choice(string.ascii_letters) for _ in range(16))
    ssrc1 = secrets.randbelow(2**32)
    ssrc2 = secrets.randbelow(2**32)
    extdata = secrets.token_hex(20) + "_0.8.5"
    lines = [
        "v=0",
        "o=- %d 2 IN IP4 127.0.0.1" % secrets.randbelow(2**63),
        "s=-",
        "t=0 0",
        "a=group:BUNDLE video",
        "a=msid-semantic: WMS stream_label0",
        "m=video 9 RTP/AVPF 96 97",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        "a=ice-ufrag:" + ufrag,
        "a=ice-pwd:" + pwd,
        "a=mid:video",
        "a=sendrecv",
        "a=rtcp-mux",
        "a=rtcp-rsize",
        "a=rtpmap:96 H264/90000",
        "a=rtcp-fb:96 ccm fir",
        "a=rtcp-fb:96 nack",
        "a=rtcp-fb:96 goog-remb",
        "a=rtcp-fb:96 transport-cc",
        "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
        "a=rtpmap:97 rtx/90000",
        "a=fmtp:97 apt=96",
        "a=ssrc-group:FID %d %d" % (ssrc1, ssrc2),
        "a=ssrc:%d cname:%s" % (ssrc1, cname),
        "a=ssrc:%d msid:stream_label0 video_label0" % ssrc1,
        "a=ssrc:%d mslabel:stream_label0" % ssrc1,
        "a=ssrc:%d label:video_label0" % ssrc1,
        "a=ssrc:%d cname:%s" % (ssrc2, cname),
        "a=ssrc:%d msid:stream_label0 video_label0" % ssrc2,
        "a=ssrc:%d mslabel:stream_label0" % ssrc2,
        "a=ssrc:%d label:video_label0" % ssrc2,
        "a=extdata:" + extdata,
    ]
    return "\r\n".join(lines) + "\r\n"


def _msg(params: dict) -> str:
    return json.dumps({"version": "1.0.1", "params": params},
                      separators=(",", ":"), ensure_ascii=False)


async def signal_probe(iot_id: str = IOT_ID) -> None:
    creds = iot_auth.load_creds()
    if not creds.get("iotToken") and creds.get("refreshToken"):
        res = iot_auth.refresh(creds["identityId"], creds["refreshToken"])
        if res["status"] != 200:
            raise SystemExit("IoT refresh failed: %s %s" % (res["status"], res["body"][:300]))
        creds.update(json.loads(res["body"]).get("data") or {})
        iot_auth.save_creds(creds)
    res = iot_auth.stream_query(iot_id, creds.get("iotToken"))
    print("stream query", res["status"], flush=True)
    if res["status"] != 200:
        raise SystemExit(res["body"][:500])
    data = json.loads(res["body"]).get("data") or {}
    p2p = data.get("p2pInfo") or {}
    url = p2p.get("signalUrl")
    print("signalUrl", url, flush=True)
    if not url:
        raise SystemExit("no signalUrl: " + json.dumps(data)[:500])

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", 0))
    lan_ip = os.environ.get("EC4_LAN_IP") or ec4_config.lan_ip()
    lport = udp.getsockname()[1]
    ufrag, pwd = _ufrag(), _pwd()
    client_id = uuid.uuid4().hex
    # The signal server validates sessionId against the URL's `session` param.
    from urllib.parse import parse_qs, urlparse
    signal_session = (parse_qs(urlparse(url).query).get("session") or [""])[0]
    if not signal_session:
        raise SystemExit("signalUrl has no session param")
    print("offer ufrag=%s pwd=%r local=%s:%d client=%s session=%s" % (
        ufrag, pwd, lan_ip, lport, client_id, signal_session), flush=True)

    offer = _msg({
        "iotId": "",
        "clientId": client_id,
        "messageType": "offerSdp",
        "clientType": "pc",
        "channelType": "websocket",
        "Version": "1.3.2",
        "sessionId": signal_session,
        "sdp": _offer_sdp(ufrag, pwd),
    })
    cand = _msg({
        "sdpMid": "video",
        "sdpMLineIndex": 0,
        "candidate": _candidate(lan_ip, lport, ufrag),
        "iotId": "",
        "clientId": client_id,
        "messageType": "candidate",
        "clientType": "pc",
        "channelType": "websocket",
        "sessionId": signal_session,
    })

    import websockets
    logf = open(OUT, "w", encoding="utf-8")
    # The app's curl sends `Expect: 101`; the signal server wants it.
    async with websockets.connect(url, max_size=2 * 1024 * 1024,
                                  additional_headers={"Expect": "101"}) as ws:
        print("wss connected", flush=True)
        await ws.send(offer)
        await ws.send(cand)
        print("offer+candidate sent", flush=True)
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=25)
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                print("<<", msg[:2000], flush=True)
                logf.write(json.dumps({"ts": asyncio.get_event_loop().time(), "msg": msg}) + "\n")
                logf.flush()
                if '"answerSdp"' in msg:
                    print("GOT ANSWER", flush=True)
        except asyncio.TimeoutError:
            print("timed out waiting for messages", flush=True)
        except Exception as exc:
            print("signal closed:", type(exc).__name__, str(exc)[:200], flush=True)
    logf.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if cmd == "probe":
        asyncio.run(signal_probe(sys.argv[2] if len(sys.argv) > 2 else IOT_ID))
    else:
        print(__doc__)
