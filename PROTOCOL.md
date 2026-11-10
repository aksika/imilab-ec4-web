# EC4 local Link Visual P2P — protocol notes

These are the reverse-engineering results from the EC4 / Imilab Home 2.9.2
investigation.  They document the **local P2P media path** and a working
Mac-side receiver.  Credentials are session-scoped; do not commit live values.

## Short version

* The camera's local stream is **plaintext H.265 in RTP**, carried over an
  Alibaba Link Visual **WebRTC/ICE** session.
* The RTP payload has a small vendor header before each H.265 NAL:
  * `0x0a` — start of a video NAL; skip **6** bytes, then the NAL begins.
  * `0x08` — continuation of the current video NAL; skip **2** bytes.
  * `0x06` / `0x09` — audio/auxiliary; skip for video.
* The hub's ICE connectivity checks can be accepted from a **Mac on the same
  LAN** using the app's ICE credentials, and the hub will send the RTP stream
  to the Mac.
* We verified this by capturing **601 RTP packets (≈669 KB) directly on the
  Mac** and reassembling 42 H.265 NALs.  The captured NALs were all
  `TRAIL_R` (type 1) because the capture joined after the stream's IDR; a
  keyframe/parameter-set source is the remaining decoder requirement.

## Where the app gets the session

The Android app calls the cloud stream-query API (`/vision/customer/stream/query`)
which returns the P2P/signal/STUN information.  The native SDK
`liblvmedia.so` (`LVMediaImpl::parseStreamQueryResponse`,
`LVLivePlayerImpl::parseStreamQueryResponse`) parses it and `LVConductor`
sets up the WebRTC/ICE transport.

Hook offsets used during this work (Imilab Home 2.9.2, `lib/lvmedia.so`):

| Function | Offset |
|---|---|
| `LVMediaImpl::parseStreamQueryResponse` | `0x1a1fa0` |
| `LVLivePlayerImpl::parseStreamQueryResponse` | `0x1864d8` |
| `StunInfo` constructor | `0x1c7cd0` |
| `LVConductor::SetCoturnInfo` | `0x22265c` |
| `IceTransportInternal::SetIceCredentials` | `0x2fb5c0` |
| `IceTransportInternal::SetRemoteIceCredentials` | `0x2fb64c` |
| `IceCredentialsChanged` | `0x2ef630` |
| `StunMessage::AddMessageIntegrity(std::string const&)` | `0x29c390` |
| `Port::SendBindingResponse` | `0x28ba94` |

The `AddMessageIntegrity` hook reveals the ICE passwords (the string is
passed directly).  In a typical session there are:

* a 16-character **STUN key** from the stream-query `stunUrl`, and
* two 22-character **ICE passwords** — one local (used by the hub when it
  sends binding checks to the app) and one remote (used by the app when it
  sends requests to the hub).

The app's local ufrag appears in the STUN USERNAME as the second component of
`hubUfrag:appUfrag` in the app's requests; the hub's checks use
`appUfrag:hubUfrag`.

## Direct Mac receiver

The experimental receiver is `tools/mac_p2p_receiver.py` (copy of the working
orchestrator).  It expects a live log from `tools/session_hooks.py`, which
spawns the app under Frida with the parse/ICE/STUN hooks and taps the first
camera tile.  The receiver then:

1. Reads the current session's 100-byte ICE binding request, 256-byte
   `startStreaming` STUN request, and the two ICE passwords from the log.
2. Sends the app's binding request from the Mac to the hub.
3. Verifies the hub's binding checks with the **local** ICE password and
   sends a signed `Binding Success` response (XOR-MAPPED-ADDRESS +
   MESSAGE-INTEGRITY + FINGERPRINT).
4. Blocks the app's outbound STUN to the hub (`iptables ... --dport <hubport>
   -j DROP`), so the hub's selected pair fails and the Mac's nominated pair
   wins.
5. Sends a `startStreaming` request (seq + 1) signed with the remote ICE
   password.
6. Receives RTP from the hub and saves it to `captures/mac_rtp.bin`.

The RTP record format in `captures/mac_rtp.bin` is:

```
>d (timestamp)  >I (length)  <UDP packet>
```

Remove the first 12 RTP bytes, then apply the `0x0a`/`0x08` header rules to
rebuild Annex-B H.265.

### STUN signing

MESSAGE-INTEGRITY is HMAC-SHA1 over the STUN message up to the
MESSAGE-INTEGRITY attribute, with the header length adjusted to the end of
that attribute.  FINGERPRINT is CRC32 over the message up to the
FINGERPRINT attribute, XOR `0x5354554e`.  The receiver implements both.

## Captured media / remaining gap

`captures/mac_rtp.bin` contains 601 RTP packets; `captures/mac_rtp.h265` is
the reassembled H.265.  The NALs are type 1 (`TRAIL_R`), so they need the
stream's VPS/SPS/PPS and an IDR to decode.  The RTP capture joined the stream
after its IDR.

To finish the decoder path, one of these is needed:

* an **IDR request** to the hub (the cloud stream query uses
  `forceIFrame: true`; the local `startStreaming` message may accept a
  similar field / extended command),
* capture the **stream start** before the hub sends its first IDR, or
* use the app's `SetExtraData` (VPS/SPS/PPS) plus the app's initial IDR as a
  one-time reference, then the Mac's subsequent RTP delta frames.

## Keyframe transport: plaintext 0x09 + tiny-AES 0xbb fragments

The main P2P video (RTP payload header `0x0a` start / `0x08` continuation) is
**delta-only** in every capture: a 140 s capture produced 8,988 RTP packets,
1,363 video NALs and **zero IRAP NALs**.  The keyframe is carried by a burst
that mixes two payload headers, all with the same SSRC/RTP timestamp and
consecutive RTP sequence numbers:

* **`0x09` fragments are plaintext.**  Payload = vendor header `09 00` +
  H.265 access-unit bytes.
* **`0xbb` fragments are AES-128-CBC encrypted per packet.**  Payload =
  `bb bb 00 00` + ciphertext (a multiple of 16 bytes) + a 3-byte plaintext
  tail.  The decrypted plaintext starts with `0b 00` (parameter sets) or
  `09 00` (access-unit data), followed by access-unit bytes.
* The first `0xbb` fragment of a keyframe decrypts to the 88-byte
  VPS/SPS/PPS parameter set, then the first bytes of the IDR NAL.  The
  remaining fragments continue the access unit.  Including the 3-byte
  plaintext tails, the fragment bytes add up exactly to
  `88 (parameter sets) + IDR NAL size`.

The decryptor is the **bundled tiny-AES** in `liblvmedia.so`, not libcrypto
and not `CStreamUnit::decryptCbc`:

* `_Z22AES_CBC_decrypt_bufferP7AES_ctxPhj` at file offset `0x30fc34`
  (`AES_CBC_decrypt_buffer`), and `_Z21AES_CTR_xcrypt_bufferP7AES_ctxPhj` at
  `0x30fdd0`; the AES S-box is present at `liblvmedia.so:0x41a4ab`.
* Context layout is the usual tiny-AES `struct AES_ctx`: round key at
  `ctx+0`, IV at `ctx+176`.  For AES-128 the first 16 round-key bytes are the
  raw key, so a hook on the decrypt function can read it directly.
* Each packet is decrypted independently with **IV = 0**.
* The key is **per stream session** and is 16 ASCII characters.  It is the
  **first 16 bytes of the video media section's ICE password** (`a=ice-pwd`
  in the remote SDP), not the stream query's `relayDecryptKey` and not the
  `key=` parameter of the p2pInfo STUN URL.
* `BaseChannel::PushdownRemoteDescription` (`0x307160`) finds the `\nvideo`
  content, copies the 16-byte string at `ContentDescription+0x48` (the
  video section's ICE password) to `BaseChannel+0x1e8`.
  `BaseChannel::HandlePacket` (`0x306798`, at `+0x134`) uses
  `BaseChannel+0x1e8` as the CBC key with a zero IV.
* The same password authenticates the camera's STUN binding requests, so the
  Mac can derive the key from the ICE/SDP exchange it already participates in
  (`tools/lv_key.py`).  No Frida key capture is required for the hybrid path.
* `CStreamUnit::decryptCbc` at `0x1c1dc8` (IV `+0xcf0`, key schedule `+0xd10`)
  belongs to the RTMP path; it is never called during P2P playback.  The
  earlier `0x09`-is-AES assumption was wrong.

Reassembly (verified byte-for-byte on every captured keyframe): sort the
burst by RTP sequence number and concatenate each fragment's access-unit
bytes (`0x09`: payload[2:]; `0xbb`: decrypt `payload[4:]` with AES-128-CBC,
IV=0, truncate to a multiple of 16, then plaintext[2:] + the remaining
3-byte tail).  For the first `0xbb` fragment, strip the `0b 00` header and
the Annex-B VPS/SPS/PPS parameter sets (the IDR NAL starts after the 4-byte
start code of the type 19/20 NAL).  The result equals the decoder's IDR NAL
exactly; `tools/analyze_keyframe.py` validates this.

Capture tooling: `tools/p2p_aes_capture.py` hooks the UDP receive path, the
decoder input, `LVLivePlayerImpl::parseStreamQueryResponse`, and the tiny-AES
decrypt functions, and records the per-session key, ciphertext and plaintext.
`tools/aes_hooks.js` remains the libcrypto/`decryptCbc` probe.
`tools/lv_key.py` derives the key from an SDP or ICE password.

## P2P control / coexistence with the app

* `stopStreaming` and `startStreaming` JSON messages sent by the Mac over the
  STUN DATA channel work: stop halts the camera's RTP and start resumes it.
* `realplay:forceIFrame` sent as a bare/JSON STUN DATA message, RTCP PLI/FIR,
  `startStreaming`, and the vendor `sendExtendedCommand` do **not** produce an
  IDR.  `LVLivePlayerImpl::sendExtendedCommand` is gated by a feature flag at
  `LVLivePlayerImpl+0x598`, which is 0 for this camera.
* The app and the Mac can coexist on the same session: instead of dropping all
  app UDP to the hub, drop only STUN packets, e.g.
  `iptables -I OUTPUT -p udp --dport <hub> -m string --algo bm --hex-string
  '|2112a442|' -j DROP`.  The hub then selects the Mac's nominated pair while
  the app stays alive and its KCP/cloud/WebSocket paths keep working.
* The app's own `startStreaming` is neutralised in-process (same-length
  `noopXXXXXXXXXX` replacement in `CStreamUnit::SendData`) so the Mac's
  request is the one the camera acts on.

## Mac media receiver (implemented)

`web/ec4_mac.py` now implements the full Mac-side path:

1. The app is spawned under Frida with `session_hooks_neutralize.js`; it logs
   in, runs the signed stream query, and opens the local P2P session.
2. The Mac captures the LAN ICE endpoint, the session ICE passwords and the
   sessionId, then sends the only real `startStreaming` request.
3. A STUN-only iptables rule keeps the app alive (KCP/cloud/WebSocket) while
   the camera sends media to the Mac.
4. The AES key is derived from the first 16 bytes of the video section's ICE
   password; `0xbb` fragments are decrypted per packet (AES-128-CBC, IV=0).
5. Keyframes are reassembled in RTP sequence order (plaintext `0x09` +
   decrypted `0xbb` + 3-byte tails), and the plaintext `0x0a`/`0x08` delta
   NALs are emitted as Annex-B.
6. The existing `Encoder` feeds ffmpeg, which produces the same H.264 fMP4
   consumed by the browser.

The website uses this path by default (`EC4_MAC=1`); `EC4_MAC=0` selects the
older Frida decoder tap.  Verified in a live run: 1280x720, zero RTP loss,
valid fMP4 that decodes cleanly.

## App-free P2P signal (ported; camera-side blocked)

`tools/p2p_signal.py` now performs the signal exchange from the Mac:

1. Refresh the IoT token and query `/vision/customer/stream/query`.
2. Connect to `p2pInfo.signalUrl` over WSS with the URL's `token`/`session`.
3. Send `offerSdp` + our own host candidate, then wait for `answerSdp` and
   device candidates.

Captured wire format (also emitted by the app's `WebsocketClient`):

```json
{"version":"1.0.1","params":{
  "iotId":"", "clientId":"<random-32-hex>",
  "messageType":"offerSdp", "clientType":"pc",
  "channelType":"websocket", "Version":"1.3.2",
  "sessionId":"<the URL session param>", "sdp":"v=0\r\n..."}}

{"version":"1.0.1","params":{
  "sdpMid":"video", "sdpMLineIndex":0,
  "candidate":"candidate:... 1 udp <prio> <ip> <port> typ host ...",
  "iotId":"", "clientId":"<same>", "messageType":"candidate",
  "clientType":"pc", "channelType":"websocket",
  "sessionId":"<the URL session param>"}}
```

Key details:

* **`sessionId` must equal the `session` query parameter of `signalUrl`.**
  A random `sessionId` makes the server drop the connection immediately.
  `clientId` is a fresh random 32-hex string.
* The WebSocket handshake must send **`Expect: 101`**.  Without it the server
  closes as soon as a text frame (the offer) arrives.  Standard curl WebSocket
  headers (`Connection: Upgrade`, `Upgrade: websocket`,
  `Sec-WebSocket-Version: 13`, `Sec-WebSocket-Key`) are used.
* Frames are **text** frames containing the JSON above.  `version` is
  `1.0.1`; the inner `Version` is `1.3.2`.
* The app's offer SDP is H.264 (`m=video 9 RTP/AVPF 96 97`, `a=sendrecv`),
  with `a=ice-ufrag`/`a=ice-pwd` and `a=extdata:<40-hex>_0.8.5`.
* The camera's `answerSdp` (when it joins) is `clientType:"dev"`,
  `a=sendonly`, and carries the camera's ICE ufrag/pwd and host candidates
  (e.g. `<CAMERA_IP>:<port> typ host`).  The first 16 bytes of the video
  section's ICE password are the `0xbb` media AES key.
* The server keeps the session for the `p2pSessionTimeout` from the stream
  query (10 s here) and then closes if the camera has not answered.

Tooling note: `tools/start_emulator.sh` now boots the AVD with
`-dns-server 8.8.8.8,1.1.1.1`.  The old emulator's slirp DNS stopped
resolving after a while (the app showed "Load failed" for
`gateway.eu-east.imilab.com`), even though the host and the emulator's
direct-IP network were fine.

**Status (2026-11-09): the signal exchange works from the Mac.**  The key
details were (a) the SDP must **not** end with a second CRLF — the camera
silently ignores an offer with the extra blank line; (b) `sessionId` must be
the URL session; (c) the handshake needs `Expect: 101`.  With those fixed,
`tools/p2p_signal.py probe` receives a real `answerSdp`, e.g.:

```
answerSdp: a=ice-ufrag:W/+o  a=ice-pwd:I21TwodQu4TB1vsq+ZoCGJHi
candidate: ... <CAMERA_IP>:41332 typ host
media AES key = first 16 bytes of the video ICE pwd
```

Wake-up is app-free too:

* `POST /thing/properties/set` with
  `{"iotId":"<camera>","items":{"SleepStatus":1}}` wakes the camera
  (`ActiveStatus` 2 = sleeping, 1 = active; `SleepStatus` 1 = sleep disabled).
* `tools/iot_auth.py wake|sleep|props|tsl` wraps this.
* The TSL (`/thing/tsl/get`) also exposes `hub_reboot`, which restarts the
  camera/gateway process; we used it to reset the device once.

**Result (2026-11-09): the Mac's own ICE/media session now works once the
camera's ICE agent is open.**  With the app playing in parallel,
`tools/p2p_media.py` captured **652 RTP packets**.  The first keyframe
decrypted/reassembled to a **1280x720 HEVC IDR (type 19, 84-byte
VPS/SPS/PPS)** and ffprobe accepts it.

Details that were required:

* **Resend the candidate after `answerSdp`.**  The camera ignores candidates
  sent before its ICE session exists.
* Match the app's captured `p100`:
  `USERNAME <camera-ufrag>:<client-ufrag>`, `0xC057` network-id/network-cost,
  `0x802A` ICE-CONTROLLED with a fixed per-session tiebreaker, priority
  `0x6E7F1EFF`, `0x0025` USE-CANDIDATE, MI with the camera's video ICE
  password.
* Send `startStreaming` **after ICE succeeds** (binding success or a verified
  camera check), not after the first RTP packet.
* Advertise a **srflx candidate** (public STUN) in addition to the host
  candidate, as the app does.  The app's offer always has both; a host-only
  candidate was never enough in the cold tests.

Verified app-free runs (app force-stopped): 845, 596, 384, 569 and 555 RTP
packets; first keyframes decrypted to 1280x720 HEVC IDR type 19 with 84-byte
VPS/SPS/PPS; ffprobe accepts them.  Latest artifact:
`captures/appfree_rtp.bin` (555 packets) and `captures/appfree_keyframe.h265`
(9211-byte IDR, ffprobe: hevc 1280x720).

## App-free starter: still the blocker

The app's native player is still the only reliable way to open the camera's
ICE agent.  The camera's agent stays warm for roughly 2–3 minutes after the
app plays; within that window `tools/p2p_media.py` works with the app fully
force-stopped.  Every cold-start path tried so far fails (camera returns
`answerSdp` and candidates, but sends no binding checks and no RTP):

* `StartP2PStreaming(StunUrl, SignalUrl)` via `/thing/service/invoke`
  (TSL service, async 200).  Tried with the full client URLs, the base
  endpoints, and after the signal `offerSdp`; the camera wakes
  (`ActiveStatus` 2 → 1 → 2) but never answers ICE.
* `StartPushStreaming(PushUrl=relayUrl, StreamType=0, Scheme=0,
  EncryptKey=relayDecryptKey, EncryptType=0, PreTime=0)` (async 200): no ICE.
* A valid relay RTMP viewer connection (ffmpeg `-rtmp_app` +
  `rtmp://host/<playpath>`): the camera **does** start pushing FLV to the
  relay (verified: encrypted HEVC/FLV data), but this does not open P2P for a
  new signal session.
* Host + srflx candidates, `USE-CANDIDATE`, and the app's exact SDP/candidate
  fields: ICE works only in the warm window.
* Link Visual STUN rendezvous: the MI key is the stream query's `key`
  parameter, the username is `client-<url session>`, and the packet is
  byte-for-byte reproducible (`HMAC-SHA1(key, header-with-MI-length + attrs)`).
  A replayed app packet gets a success response, but a freshly queried Mac
  session times out — the STUN server only knows sessions where the camera is
  already active/registered.  So this is a consequence, not the starter.

The app's starter is cloud-side/native: we captured its OkHttp calls, native
TLS traffic and Paho MQTT publishes; during play it sends only the stream
query, the P2P signal offer/candidates, the STUN rendezvous and the relay RTMP
play.  It does not call `StartP2PStreaming`/`StartPushStreaming` itself (the
cloud does).  The camera-side trigger is not any of the client-visible
commands tried above.

To use the app-free media path today:

```bash
# starter: let the app play the camera once (or use the hybrid broker)
.venv/bin/python tools/iot_auth.py wake
.venv/bin/python tools/p2p_media.py 25   # within ~2-3 min of the app's play
```

## MITM of the camera's cloud link (2026-11-10)

A root ARP-spoof capture (`tools/mitm_capture.py`) of CAM2 during an app play
confirmed the camera's command path is encrypted:

* Camera cloud MQTT: TCP **1883 to 8.209.64.105**, but the payload is
  **TLS 1.3** (`17 03 03 ...` records), so the start command is not visible.
* Camera relay push: TCP 8000 to 47.254.164.212 (RTMP, obfuscated/encrypted).
* Camera HTTP: TCP 80 to 103.235.47.188; STUN: UDP 3478 to 8.219.102.239.
* The capture also contains the app's `startStreaming` STUN DATA (session
  `af7046d4...`) and the camera's RTP to the Mac/emulator.
* The hub at <HUB_IP>:1883 is a separate MQTT broker with auth; the camera
  does not use it.
* Local CoAP 5683 answers `4.04` for `/dev/core/service/dev`; ALCS multicast
  224.0.1.187/.188 gets no reply; camera TCP 8001-8004 accept but are passive.
* `/living/alcs/localgroup/query` is empty; no local group exists.

Conclusion: the cloud→camera starter is not visible on the encrypted camera
link, but it is **not needed**: a correctly reproduced private relay viewer
(`glive`, RTMP C0=4, one-chunk connect, Ping.Start) opens the camera's P2P
agent from the Mac.  Camera root is not required.  See the pure-Mac section
below.

Capture: `captures/ec4_mitm.pcap` (mode 600).

## App player reverse (2026-11-10)

The app's live-view entry point is now mapped and callable without UI taps.

### Call chain

```
ALLivePlayerImpl.setDataSource(Bundle)
  -> LVLivePlayer.setLiveDataSource(iotId, LV_STREAM_TYPE_MAJOR, 2000)
     -> native_set_live_data_source_iotid_streamtype_cacheduration(...)
LVLivePlayer.start()
  -> native_start(...)
```

The corresponding cloud call is `APIHelper.sendIoTRequest` with:

* `/vision/customer/sls/token/query` (`1.0.2`, `{}`)
* `/vision/customer/stream/query` (`2.2.0`) with exactly:
  `iotId`, `streamType=0`, `relayEncrypted=true`, `relayEncryptType=0`,
  `cacheDuration=2000`, `forceIFrame=true`, `needDomainName=true`,
  `enableWebSocket=true`, `enablePortPredict=true`, `clientType=Android`.

There is **no separate app-side start request**.  During a play the app only
issues those two APIs, then opens the signal WebSocket and ICE.  The cloud
opens the camera's P2P agent in response to the trusted player session; the
app never sees that command.

### Battery-camera gate

The app **does not wake a sleeping camera**.  If `SleepStatus=0`, its player
logs `start()` then `stop#()` / `STATE_ENDED` within ~0.5 s and the tile shows a
cached preview with a pause icon.  The correct order is:

1. Mac sends `SleepStatus=1` via the IoT property API.
2. Wait until `ActiveStatus=1`.
3. Start the app player.
4. Mac takes over the media transport.

### Headless starter

`tools/frida_player_bridge.js` (bundled as
`tools/frida_player_bridge.bundle.js` because Frida 17 has no global `Java`)
creates the app's own `LVLivePlayer` with the application context:

```js
var player = LVLivePlayer.$new(appContext);
player.setLiveDataSource(iotId, LVStreamType.parseInt(0), 2000);
player.start();
```

`prepare()` creates the player and loads `liblvmedia.so`; `play(iotId)` sets
the source and starts; `stop()` stops/releases it.  This replaces the home-tile
tap and works with the existing `session_hooks_neutralize.js` hooks.

### Verified end-to-end

Cold camera (asleep) -> IoT wake -> headless Frida player -> session/ICE hooks
-> Mac takes over and sends `startStreaming`:

* `POST /api/select {"name":"CAM2"}` returned `1280x720`.
* Mac receiver logged `media ssrc`, `media key`, a 58 KB keyframe at RTP 128,
  `first keyframe ... params 88B`, and `encoder mac 1280x720`.
* Status: `rtp=131`, `keyframes=1`, `deltas=44`, no sequence gaps.
* Cold start time: **40.6 s** (app spawn, wake, player start, signal, keyframe).

The app is still required as the P2P opener.  This removes the UI automation,
not the app itself.

## Pure-Mac relay + P2P capture (2026-11-10)

This is the final working path: **no Android app and no emulator**.

The relay is a valid opener if the private RTMP client is reproduced exactly:

* RTMP app name is `glive?token=...`; `tcUrl` remains
  `rtmp://.../live?...`.
* Client handshake is **C0 = 0x04**, then C1 = `times()` milliseconds,
  four zero bytes, 1528 random bytes.  The `connect` packet is sent **in one
  RTMP chunk** (no 128-byte continuation chunks).
* The playpath is the relay path **without** a leading `-`.
* `NetStream.RequestAudioType` and `NetStream.Ping.Start` must include the
  AMF `null` marker before the object; omitting it makes the relay reset.
* Keeping this private viewer attached opens the camera's P2P agent.  The Mac
  then sends its own offer/candidates and `StartP2PStreaming`, answers the
  camera's ICE checks, and receives RTP directly.

Implementation:

* `tools/rtmp_private_viewer.py` — pure-Mac private RTMP relay viewer.
* `tools/direct_signal.py` — stream query, private viewer trigger, signal/ICE
  negotiation, `StartP2PStreaming`.
* `web/ec4_direct.py` — `DirectCapture`: viewer + signal + `_Receiver` +
  ffmpeg; selected by default with `EC4_DIRECT=1`.
* `web/ec4_server.py` — serves `/api/select`, `/api/status` and
  `/stream/direct` from the direct path.

Verified with the Android emulator **killed** (`adb devices` empty):

* Cold `POST /api/select {"name":"CAM2"}`: **4.7–9.1 s**.
* `1280x720`, `connected_app=false`, `broker=false`.
* `/stream/direct` starts with `ftyp` + `moov`; status reached 121 frames,
  `init=true`, RTP 368, 2 keyframes, no sequence gaps.

Media pipeline note: the ffmpeg input must **not** use `-fflags nobuffer`.
With `-use_wallclock_as_timestamps 1` it drops HEVC reference frames and
produces grey/purple macroblocks (especially at the bottom of the first
keyframe).  `Encoder.feed()` also loops on `stdin.write()` so a large IDR is
never partially written.

The camera runtime is fully Mac-native.  The only cloud token dependency is
the Alibaba IoT credential pair in `~/.ec4/iot_creds.json`; it can now be
bootstrapped from the Imilab username/password login (below) or refreshed
without the app.  The original app bootstrapped token remains a fallback.

## Web login + Keychain (2026-11-10)

* `web/static/settings.html` — normal login form at `/settings`; the browser
  may offer to save the password in its own password manager.
* `tools/imi_login.py` — Imilab password login:
  * `GET /oauth/login/tick` returns an RSA public key.
  * Encrypt `{"account","password"}` with RSA/PKCS1 and POST to
    `oauth/enter/v3/prefix/login`.
  * Saves the Imilab access/refresh tokens to `~/.ec4/imi_tokens.json`.
  * Exchanges `oauth/sdk/login` (`sign` = base64(HMAC-SHA256(`imiUid+timestamp+appId`),
    key hidden in `imi_security.jpg`)) for an OpenAccount session:
    * `POST https://living-account.eu-central-1.aliyuncs.com/api/prd/loginbyoauth.json`
      with `oauthPlateform=23`, `oauthAppKey=<IOT_APP_KEY>`,
      `authCode=<Imilab authCode>`.
    * The gateway signature is HMAC-SHA1 with the recovered seed
      `<IOT_XCA_KEY>`, `x-ca-key=<IOT_APP_KEY>`; the signed
      form value is the **raw JSON** (only the transmitted body is URL-encoded).
    * `POST /account/createSessionByAuthCode` on the EU IoT gateway with
      `authCode=<sid>`, `appKey=<IOT_APP_KEY>`, `accountType=OA_SESSION` returns
      `identityId`, `iotToken`, `refreshToken`.
  * `python tools/imi_login.py --bootstrap` runs this from the saved Imilab
    token; `--refresh` rotates the IoT token; `--status` shows state.
* `tools/mac_keychain.py` — stores the username/password in the macOS Keychain
  via `keyring` (service `ec4-imilab`).  The browser stores only a local
  `ec4_session` HttpOnly cookie; cloud credentials are never in a cookie.
* `POST /api/auth/login` / `/api/auth/status` / `/api/auth/logout`.

The password endpoint is verified (a bad user returns “user does not exist”).
The full password → Imilab token → OpenAccount `sid` → IoT credential exchange
is implemented and verified: the resulting token returns HTTP 200 from
`/living/user/profile/get` and `/thing/properties/get`.  `web/ec4_direct.py`
calls `ensure_iot_token()` before opening a stream, which refreshes the IoT
token and falls back to a Keychain re-login when needed.

The global `sdk.openaccount.aliyun.com` host creates a session the EU IoT
gateway cannot see (`2459 会话 Id 不存在`); the EU host above is required.

## Next step

1. The app-free signal, wake, ICE, `startStreaming`, RTP, keyframe and ffmpeg
   paths are done; the direct path is the website default (`EC4_DIRECT=1`).
2. The Imilab password → OpenAccount → IoT credential exchange is done and
   verified.  Sign in once at `/settings` with the real account password to
   populate the macOS Keychain; after that the helper can refresh and, if
   needed, re-login without the app.
3. The app/emulator fallback (`EC4_DIRECT=0 EC4_MAC=1`) remains available but
   is not used by the normal launchers.
4. Optional cleanup: remove stale hybrid-only tooling and docs once the web
   login has been used successfully with the real password.
## Cloud auth: what is portable and what is not

### Imilab account (portable)

* The app stores an Imilab JWT access token and a 14-day refresh token in
  `IMI_sp_key_Name.xml` (`imi_sp_user_token_data_key`).
* `GET https://gateway.eu-east.imilab.com/oauth/sdk/refreshToken?refreshToken=...`
  works **without** `X-IMILAB-SIGN`; the Mac can refresh indefinitely and
  store the tokens in `~/.ec4/imi_tokens.json` (`tools/imi_auth.py`).
* `GET /api/app/storage/device/v1/allDevices` works with `X-IMILAB-TOKEN` +
  `X-IMILAB-IDENTITYID` and **no signature**.  The identity ID is
  `<IOT_IDENTITY_ID>` for this account.
* Other Imilab gateway endpoints may still require the white-box
  `X-IMILAB-SIGN`; test each endpoint.

### Alibaba IoT (cracked)

* The AEP SDK holds the Alibaba `iotToken`, `refreshToken` and `identityId`
  (`TokenUtils.getAliToken()`, `getIdentityId()`; read via Frida once).
* The API Gateway `x-ca-signature` is **HMAC-SHA1** with the per-install seed
  key `<IOT_XCA_KEY>` (ASCII, 32 chars).  The key was
  recovered from the SG native signer by tracing `memcpy` during a signing
  call and testing candidate buffers against the known HMAC output.  It is
  stable across app restarts (verified) but is device/install-specific.
* StringToSign (exactly as the SDK builds it):

  ```
  POST
  application/json; charset=utf-8
  <base64(md5(body))>
  application/octet-stream; charset=utf-8
  <RFC1123 date>
  x-ca-key:<IOT_APP_KEY>
  x-ca-nonce:<uuid>
  x-ca-signature-method:HmacSHA1
  x-ca-timestamp:<ms>
  <path>?x-ca-request-id=<uuid>
  ```

  `x-ca-signature` = base64(HMAC-SHA1(key, StringToSign)).  Verified against
  two captured signatures byte-for-byte.
* `tools/iot_auth.py` now refreshes the IoT token and queries
  `/vision/customer/stream/query` from the Mac.  A real query returns the
  `p2pInfo.signalUrl` / `stunUrl`, `relayUrl` and `relayDecryptKey` with no
  app involved.

### Media bootstrap

* The Mac P2P stream **survives force-stopping the app**: RTP, keyframes and
  ffmpeg fragments continued after `am force-stop` (tested 20 s+).  The app
  is only needed to obtain the signed stream session/ICE bootstrap; it is
  not needed to keep media alive.

### Remaining options

1. Keep the app as an **on-demand bootstrap/signing broker**: start it only
   when a new stream session or IoT token is needed; the Mac keeps media
   alive and switches between cached sessions.
2. White-box reverse of SecurityGuard/IMISecurity (high effort, uncertain).
3. Probe for unsigned cloud endpoints (Imilab refresh and device list
   already work unsigned).

## Files

* `captures/mac_rtp.bin` — RTP packets received directly by the Mac.
* `captures/mac_rtp.h265` — reassembled H.265 NALs (delta frames only).
* `tools/rtmp_private_viewer.py` — pure-Mac private `glive` RTMP relay
  viewer (C0=4, one-chunk connect, play, Ping.Start).
* `tools/direct_signal.py` — app-free stream query + private relay trigger +
  signal/ICE negotiation.
* `web/ec4_direct.py` — pure-Mac `DirectCapture` backend (viewer + receiver +
  ffmpeg); default website mode.
* `tools/imi_login.py` — Imilab password login + token persistence.
* `tools/mac_keychain.py` — macOS Keychain storage for the helper credentials.
* `web/static/settings.html` — web login form (`/settings`).
* `tools/session_hooks.py` — spawn the app with parse/ICE/STUN hooks.
* `tools/session_hooks.js` — the Frida hooks used by the above.
* `tools/session_hooks_neutralize.js` — neutralise the app's own
  `startStreaming` so the Mac sends the real one.
* `tools/frida_player_bridge.js` — headless `LVLivePlayer` start/stop/state
  bridge; `tools/frida_player_bridge.bundle.js` is the Frida 17 bundle loaded
  by `web/ec4_mac.py`.
* `tools/java_api_mqtt.js` — Java capture for `APIHelper.sendIoTRequest`,
  the stream-query response, MQTT publishes and `CloudUtils.invokeService`.
* `tools/frida_player_start.js` — first standalone direct-player probe.
* `tools/mac_p2p_receiver.py` — Mac-side ICE nomination + RTP receiver.
* `tools/hybrid_bootstrap.py` — session bootstrap, STUN-only coexistence,
  Mac nomination, start/stop control, RTP capture and app NAL capture.
* `tools/p2p_signal.py` — app-free WSS signal client: stream query, offer,
  candidate, answer/candidate capture.
* `tools/p2p_media.py` — app-free signal + ICE + `startStreaming` + RTP
  capture + keyframe decrypt/reassemble.  Requires the app/hybrid session as
  a P2P starter.
* `tools/p2p_stream_test.py` — earlier signal + ICE/startStreaming/RTP
  experiment (kept as a diagnostic).
* `tools/iot_auth.py wake|sleep|props|tsl` — app-free camera wake, property
  read/set and TSL fetch (`SleepStatus`, `ActiveStatus`, `hub_reboot`).
* `tools/signal_capture*.js` / `tools/signal_capture.py` — diagnostic hooks
  for the signal URL, `WebsocketClient` frames and the native signal handler.
* `tools/p2p_aes_capture.py` — UDP + decoder NAL + stream query +
  tiny-AES key/plaintext capture.
* `tools/analyze_aes.py` — offline AES burst decryption / IDR comparison.
* `tools/aes_hooks.js` — libcrypto and `decryptCbc` probes (RTMP path).
* `tools/force_iframe_test.py`, `tools/rtcp_keyframe_test.py`,
  `tools/force_iframe_live.py` — keyframe-request experiments.
* `tools/frida_java/` — Frida 17 Java-bridge scripts and rebuild notes.
* `tools/ice_hooks.js`, `tools/parse_hooks.js` — smaller diagnostic hooks.

The live session credentials are ephemeral.  The hook scripts print them to
the terminal/log; treat those logs as sensitive and do not commit them.
