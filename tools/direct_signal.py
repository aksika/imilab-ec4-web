"""Pure-Mac Link Visual session negotiation (no app, no emulator).

The camera's P2P agent is opened by a private `glive` RTMP relay viewer.  This
module performs the same stream query, offer/answer, ICE candidate exchange
and `StartP2PStreaming` call as the app, then returns the credentials the Mac
receiver needs.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import iot_auth  # noqa: E402
import p2p_media  # noqa: E402
import p2p_signal  # noqa: E402
import ec4_config  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

IOT_ID = os.environ.get("EC4_IOT_ID") or ec4_config.first_camera_id()
LAN_IP = ec4_config.lan_ip()


async def negotiate(url: str, stun: str, sess: str, iot: str,
                    log=print) -> dict:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((LAN_IP, 0))
    lport = udp.getsockname()[1]
    pub = p2p_media.public_stun(udp)
    udp.close()
    log(f"public stun {pub}")

    ufrag, pwd = p2p_signal._ufrag(5), p2p_signal._pwd()
    tiebreaker = int.from_bytes(os.urandom(8), "big")
    offer = p2p_signal._msg({
        "iotId": "", "clientId": sess, "messageType": "offerSdp",
        "clientType": "pc", "channelType": "websocket", "Version": "1.3.2",
        "sessionId": sess, "sdp": p2p_signal._offer_sdp(ufrag, pwd),
    })
    cand_list = [p2p_signal._msg({
        "sdpMid": "video", "sdpMLineIndex": 0,
        "candidate": p2p_signal._candidate(LAN_IP, lport, ufrag),
        "iotId": "", "clientId": sess, "messageType": "candidate",
        "clientType": "pc", "channelType": "websocket", "sessionId": sess,
    })]
    if pub:
        mapped = "%s:%d" % pub
        srflx = ("candidate:%d 1 udp 1686052607 %s %d typ srflx raddr %s rport %d "
                 "generation 0 ufrag %s network-id 1 network-cost 50") % (
            int.from_bytes(os.urandom(4), "big"), pub[0], pub[1],
            LAN_IP, lport, ufrag)
        cand_list.append(p2p_signal._msg({
            "sdpMid": "video", "sdpMLineIndex": 0, "candidate": srflx,
            "iotId": "", "clientId": sess, "messageType": "candidate",
            "clientType": "pc", "channelType": "websocket", "sessionId": sess,
            "priority": 1, "simpleNatType": 0,
            "mappedAddressList": [mapped, mapped, mapped],
        }))

    answer = None
    cands = []
    async with connect(url, max_size=2**21,
                       additional_headers={"Expect": "101"}) as ws:
        await ws.send(offer)
        for c in cand_list:
            await ws.send(c)
        starter = iot_auth.start_p2p_streaming(IOT_ID, iot, stun, url)
        log(f"StartP2PStreaming {starter['status']}")
        t0 = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - t0 < 6:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            p = json.loads(msg).get("params") or {}
            if p.get("messageType") == "answerSdp":
                answer = p.get("sdp")
            elif p.get("messageType") == "candidate":
                cands.append(p.get("candidate"))
            if answer and p2p_media.pick_host(cands):
                break
        if not answer:
            raise RuntimeError("no answerSdp from camera signal")
        for c in cand_list:
            await ws.send(c)
        await asyncio.sleep(0.2)
        cu = re.search(r"a=ice-ufrag:(\S+)", answer).group(1)
        cp = re.search(r"a=ice-pwd:(\S+)", answer).group(1)
        host = p2p_media.pick_host(cands)
        if not host:
            raise RuntimeError("camera answer had no LAN host candidate")
        log(f"camera ufrag={cu} host={host}")
        p100 = p2p_media.binding_request(ufrag, cu, cp, tiebreaker)
        return {
            "hub": host,
            "pws": [pwd, cp],
            "p100": p100,
            "session_id": sess,
            "camera_ufrag": cu,
            "camera_pwd": cp,
            "local_pwd": pwd,
            "tiebreaker": tiebreaker,
        }


def query_session(iot_id: str = IOT_ID) -> tuple[dict, str, str, str, str]:
    """Refresh the IoT token and return (data, signal, stun, session, token)."""
    creds = iot_auth.load_creds()
    if creds.get("refreshToken") and creds.get("identityId"):
        r = iot_auth.refresh(creds["identityId"], creds["refreshToken"])
        if r["status"] == 200:
            creds.update(json.loads(r["body"]).get("data") or {})
            iot_auth.save_creds(creds)
    iot = creds.get("iotToken")
    if not iot:
        raise RuntimeError("no IoT token; run the app once to bootstrap")
    q = iot_auth.stream_query(iot_id, iot)
    data = json.loads(q["body"]).get("data") or {}
    p2p = data.get("p2pInfo") or {}
    signal = p2p.get("signalUrl") or ""
    stun = p2p.get("stunUrl") or ""
    sess = urllib.parse.parse_qs(
        urllib.parse.urlparse(signal).query
    ).get("session", [""])[0]
    if not signal or not sess:
        raise RuntimeError(f"stream query failed: {q['body'][:200]}")
    return data, signal, stun, sess, iot


def negotiate_sync(url: str, stun: str, sess: str, iot: str, log=print) -> dict:
    return asyncio.run(negotiate(url, stun, sess, iot, log))
